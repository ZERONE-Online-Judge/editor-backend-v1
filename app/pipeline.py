import copy
import shlex
import math
import re
import time
import threading
from pathlib import Path
from .db import SessionLocal, User
from .parallel import ordered_work
from .domain import expanded_tests
from .sandbox import Sandbox, SandboxError, VENDOR
from .storage import put_blob, read_blob
from .scoring import score_results
from .execution_limits import MAX_GENERATOR_ARGUMENTS, GENERATOR_CPU_MS, GENERATOR_MEMORY_MB, GENERATOR_OUTPUT_BYTES
from .stress_search import StressSearch
from .expectations import expected_tag, evaluate as evaluate_expectations, matches
from .invocation_scope import selected_testsets


class Pipeline:
    def __init__(self, job, publish, check_cancelled):
        self.job = job
        self.doc = job.document
        self.publish = publish
        from .config import get_settings
        context = None
        if get_settings().execution_transport == "agent":
            with SessionLocal() as db:
                user = db.get(User, job.user_id)
                if not user:
                    raise SandboxError("실행 요청 계정을 찾을 수 없습니다.")
                context = {"email": user.email, "job_kind": job.kind, "runtime_revision": job.options.get("execution_revision", "editor-agent-v1")}
        self.parallelism = min(get_settings().test_parallelism, get_settings().execution_concurrency)
        self.program_lock = threading.RLock()
        self.blob_lock = threading.Lock()
        self.sandbox = Sandbox(check_cancelled, remote_context=context)
        self.result = {"summary": "자료를 준비하고 있습니다.", "progress": 0, "events": [], "testsets": {}, "solutions": [], "unit_tests": []}
        if job.options.get("scope"):
            self.result["scope"] = copy.deepcopy(job.options["scope"])
        self.result["parallelism"] = self.parallelism
        self.programs = {}
        self.counter = 0
        self.last_publish = 0
        self.result_lock = threading.RLock()
        runtime = {"backend": "agent" if self.sandbox.agent_transport else "editor", "image": self.sandbox.settings.runner_image, "image_id": self.sandbox.resolve_image(self.sandbox.settings.runner_image), "cpu_measurement": "native-command-and-descendants"}
        if self.sandbox.agent_transport:
            runtime["runtime_revision"] = context["runtime_revision"]
            runtime.pop("image_id", None)
            runtime.pop("image", None)
            self.result["execution_runs"] = []
            def record_execution(record):
                with self.result_lock:
                    self.result["execution_runs"].append(record)
                    self.result["execution_runs"] = self.result["execution_runs"][-2000:]
                    self.publish(copy.deepcopy(self.result))
            self.sandbox.agent_transport.on_result = record_execution
        self.remote_judging = job.options.get("execution_backend") == "zoj" and job.kind in {"verify", "package"}
        self.result["runtime"] = {"backend": "zoj", "runtime_revision": job.options.get("judge_revision")} if self.remote_judging else runtime
        if self.remote_judging:
            self.result["preparation_runtime"] = runtime
        if job.kind in {"prepare", "verify", "package"}:
            from .preparation_cache import PreparationCache
            self.preparation_cache = PreparationCache(self)


    def event(self, label, **extra):
        with self.result_lock:
            self.result["events"].append({"label": label, **extra})
            self.result["summary"] = label
            self.result["events_total"] = self.result.get("events_total", 0) + 1
            self.result["events"] = self.result["events"][-2000:]
            if time.monotonic() - self.last_publish >= 1:
                self.publish(copy.deepcopy(self.result))
                self.last_publish = time.monotonic()

    def blob(self, content):
        with self.blob_lock, SessionLocal() as db:
            value = put_blob(db, self.job.problem_id, content)
            db.commit()
            return value["sha256"]

    def work(self, function, items):
        return ordered_work(function, items, getattr(self, "parallelism", 1), self.sandbox.check_cancelled)

    def program(self, name, solution=False):
        with self.program_lock:
            return self._program(name, solution)

    def _program(self, name, solution=False):
        key = (name, solution)
        if key in self.programs:
            return self.programs[key]
        sources = self.doc["solutions"] if solution else self.doc["files"]
        if name.startswith("std::"):
            standard = name.removeprefix("std::")
            path = VENDOR / (standard + ".cpp")
            content = path.read_bytes()
            source = {"name": standard + ".cpp", "kind": "source", "language": "cpp17", "sha256": self.blob(content), "size": len(content)}
        else:
            if name not in sources:
                raise SandboxError(f"실행 소스를 찾을 수 없습니다: {name}")
            source = sources[name]
        self.event(f"컴파일 · {source['name']}")
        program = self.sandbox.compile(source, list(self.doc["files"].values()), is_solution=solution)
        self.programs[key] = program
        return program

    def generator(self, command):
        tokens = shlex.split(command)
        if not tokens or len(tokens) > MAX_GENERATOR_ARGUMENTS + 1:
            raise SandboxError(f"생성기와 최대 {MAX_GENERATOR_ARGUMENTS}개 인자를 입력하세요.")
        name = tokens[0]
        if name not in self.doc["files"]:
            matches = [n for n in self.doc["files"] if Path(n).stem == name]
            if len(matches) != 1:
                raise SandboxError(f"생성기 파일을 찾을 수 없습니다: {name}")
            name = matches[0]
        program = self.program(name)
        result = self.sandbox.run(program, args=tokens[1:], cpu_ms=GENERATOR_CPU_MS, memory_mb=GENERATOR_MEMORY_MB, output_bytes=GENERATOR_OUTPUT_BYTES)
        if result["status"] != "ok" or result["exit_code"] != 0:
            raise SandboxError(f"입력 생성 실패: {command}\n{result['stderr']}")
        return result["output"]

    def validator(self, input_bytes, testset="tests", group=""):
        names = [self.doc["validator"], *self.doc["extra_validators"]]
        if not names[0]:
            raise SandboxError("validator를 선택하세요.")
        for name in names:
            program = self.program(name)
            args = ["--testset", testset]
            if group:
                args += ["--group", group]
            result = self.sandbox.run(program, input_bytes, args=args, cpu_ms=10000, memory_mb=512)
            if result["status"] != "ok" or result["exit_code"] != 0:
                return False, result
        return True, result

    def checker(self, input_bytes, actual, answer, points=0):
        program = self.program(self.doc["checker"])
        result = self.sandbox.run(program, files={"test.in": input_bytes, "test.out": actual, "test.ans": answer}, args=["test.in", "test.out", "test.ans"], cpu_ms=10000, memory_mb=512)
        if result["status"] != "ok":
            return "JUDGE_ERROR", result
        verdict = {0: "AC", 1: "WA", 2: "PE", 3: "JUDGE_ERROR"}.get(result["exit_code"], "JUDGE_ERROR")
        result["score"] = points if verdict == "AC" else 0
        if result["exit_code"] == 7 and self.doc["info"]["points_enabled"]:
            match = re.search(r"\bpoints\s+([0-9.eE+-]+)", result["stderr"])
            raw = float(match.group(1)) if match else float("nan")
            maximum = 100 if self.doc["info"]["checker_percent"] else points
            if not math.isfinite(raw) or raw < 0 or raw > maximum + 1e-8:
                return "JUDGE_ERROR", result
            result["score"] = points * raw / 100 if self.doc["info"]["checker_percent"] else raw
            verdict = "AC" if raw >= maximum - 1e-8 else "PARTIAL" if raw > 0 else "WA"
        return verdict, result

    def run_solution(self, name, input_bytes, answer=b"", preparation=False):
        info = self.doc["info"]
        program = self.program(name, True)
        cpu_ms, memory_mb = info["time_limit_ms"], info["memory_limit_mb"]
        if preparation and self.remote_judging:
            # This creates reference answers on the selected execution VM. The
            # requested problem limits are enforced later by the ZOJ agent.
            cpu_ms = min(60000, max(10000, cpu_ms * 10 + 3000))
            memory_mb = max(1024, memory_mb * 2 + 256)
        if info["interactive"]:
            actor = self.program(self.doc["interactor"])
            result = self.sandbox.interactive(program, actor, input_bytes, answer, cpu_ms=cpu_ms, memory_mb=memory_mb)
        else:
            result = self.sandbox.run(program, input_bytes, cpu_ms=cpu_ms, memory_mb=memory_mb, input_file=info["input_file"], output_file=info["output_file"])
        status = result["status"]
        if status != "ok":
            verdict = {"time-limit": "TLE", "memory-limit": "MLE", "output-limit": "OLE", "wall-limit": "IDLENESS_LIMIT" if info["interactive"] else "JUDGE_ERROR"}.get(status, "JUDGE_ERROR")
        elif info["interactive"] and result.get("stopped_by_interactor"):
            verdict = {1: "WA", 2: "PE"}.get(result.get("interactor_exit_code"), "JUDGE_ERROR")
        elif result["exit_code"] != 0:
            verdict = "RE"
        elif info["interactive"]:
            verdict = {0: "AC", 1: "WA", 2: "PE"}.get(result.get("interactor_exit_code"), "JUDGE_ERROR")
        else:
            verdict = "AC"
        return verdict, result

    def main(self):
        names = [name for name, s in self.doc["solutions"].items() if s["tag"] == "MA"]
        if len(names) != 1:
            raise SandboxError("기대 출력을 생성할 Main 풀이를 지정하세요.")
        return names[0]

    def prepare(self):
        mode = self.job.options.get("prepare_mode", "full") if self.job.kind == "prepare" else "full"
        main = self.main() if mode in {"full", "answers"} else None
        self.result["prepare_mode"] = mode
        frozen = self.job.kind == "verify" and self.job.options.get("snapshot") == "committed"
        selected = selected_testsets(self.doc, self.job.options, self.job.kind)
        self.result["processed_testsets"] = selected
        for set_name in selected:
            testset = self.doc["testsets"][set_name]
            saved = expanded_tests(testset)
            prepared = []
            indices = self.job.options.get("test_indices") if self.job.kind in {"verify", "prepare"} else []
            seen = {test["input_sha"] for test in saved if self.job.kind == "prepare" and mode in {"full", "inputs"} and indices and test["index"] not in indices and test["input_sha"]}
            from .test_preparation import prepare_test
            def prepare_case(test):
                return prepare_test(self, test, set_name, mode, frozen, main)
            chosen = [test for test in sorted(saved, key=lambda t:t["index"]) if not indices or test["index"] in indices]
            with self.work(prepare_case, chosen) as results:
                for row, sha, input_bytes, answer in results:
                    if sha in seen and not self.doc["info"]["skip_duplicate_validation"]:
                        raise SandboxError(f"{set_name} #{row['index']}: 중복된 입력입니다.")
                    seen.add(sha)
                    if input_bytes is not None:
                        row["input_sha"] = self.blob(input_bytes)
                    if answer is not None:
                        row["answer_sha"] = self.blob(answer)
                    prepared.append(row)
            self.result["testsets"][set_name] = prepared
        label = {'inputs':'입력 재생성과 Validator 검사 완료','validate':'저장된 입력 Validator 검사 완료','answers':'저장된 입력의 Main 정답 재생성 완료'}.get(mode, '커밋된 입력·기대 출력 확인 완료' if frozen else '입력 검증과 Main 기대 출력 생성 완료')
        cache = getattr(self, "preparation_cache", None)
        if cache:
            reused = sum(cache.stats["reused"].values())
            executed = sum(cache.stats["executed"].values())
            label = f"테스트 자료 준비 완료 · {reused}개 단계 재사용 · {executed}개 단계 실행"
        self.event(f"{label} · {', '.join(selected)}")

    @staticmethod
    def expected_matches(tag, verdicts):
        return matches(tag, verdicts)

    def verify(self):
        from .verification_progress import VerificationProgress
        from .expectations import VERSION
        self.result["expectations_version"] = VERSION
        selected = list(self.doc["solutions"]) if self.job.kind == "package" else self.job.options["solutions"] or list(self.doc["solutions"])
        self.verification = VerificationProgress(self, selected)
        self.prepare()
        self.verification.begin()
        if self.remote_judging:
            from .judge_bridge import verify
            verify(self)
            return
        tasks = [(name, set_name, test) for name in selected if self.doc["solutions"][name]["tag"] != "NR"
                 for set_name, tests in self.result["testsets"].items()
                 if set_name in self.verification.planned
                 for test in tests if (self.job.kind == "package" or not self.job.options["test_indices"] or test["index"] in self.job.options["test_indices"])
                 and expected_tag(self.doc["solutions"][name], set_name, test["group"], self.doc["testsets"])[0] != "NR"]
        score_scope = {name: expanded_tests(self.doc["testsets"][name]) for name in self.result["testsets"]}
        def finish_solution(name):
            with self.verification.lock:
                source, rows = self.doc["solutions"][name], self.verification.rows[name]["cases"]
                if source["tag"] == "NR":
                    self.verification.finish_solution({"name": name, "tag": "NR", "matched": True, "cases": [], "skipped": True})
                else:
                    expectations = evaluate_expectations(self.doc, source, self.result["testsets"], rows)
                    self.verification.finish_solution({"name": name, "tag": source["tag"], **expectations, "cases": rows, "skipped": False, **score_results(self.doc, score_scope, rows)})
        def verify_case(task):
            name, set_name, test = task
            self.verification.started(name)
            self.event(f"실제 실행 · {name} / {set_name} #{test['index']}")
            inp, ans = read_blob(test["input_sha"]), read_blob(test["answer_sha"])
            verdict, result = self.run_solution(name, inp, ans)
            checked = {"score": 0}
            comment = result.get("stderr", "")
            if verdict == "AC":
                verdict, checked = self.checker(inp, result["output"], ans, test["points"])
                comment = checked["stderr"]
            row = {"testset": set_name, "index": test["index"], "verdict": verdict,
                "cpu_ms": result["cpu_ms"], "wall_ms": result["wall_ms"], "memory_kb": result["memory_kb"],
                "comment": comment[:2048], "score": checked.get("score", 0) if verdict in {"AC", "PARTIAL", "WA", "PE"} else 0,
                "group": test["group"], "max_score": test["points"], "input_sha": test["input_sha"],
                "answer_sha": test["answer_sha"], "actual_sha": self.blob(result["output"])}
            if self.verification.case(name, row):
                finish_solution(name)
        with self.work(verify_case, tasks) as results:
            for _ in results:
                pass
        for name in selected:
            if self.verification.rows[name]["state"] != "completed":
                finish_solution(name)
        mismatches = [s["name"] for s in self.result["solutions"] if not s["matched"]]
        self.result["verified"] = not mismatches and all(s.get("expectations_complete", True) for s in self.result["solutions"])
        self.result["mismatches"] = mismatches
        self.verification.finish()
        self.event("풀이 기대 판정 확인 완료" if not mismatches else "기대 판정과 다른 풀이가 있습니다.")

    def unit_tests(self, kind):
        tests = self.doc["validator_tests" if kind == "validator-tests" else "checker_tests"]
        def component_case(item):
            key, test = item
            self.event(f"구성요소 테스트 · #{key}")
            inp = read_blob(test["input_sha"])
            if kind == "validator-tests":
                valid, result = self.validator(inp)
                verdict = "VALID" if valid else "INVALID" if result["status"] == "ok" and result["exit_code"] >= 0 else "CRASHED"
            else:
                verdict, result = self.checker(inp, read_blob(test["output_sha"]), read_blob(test["answer_sha"]))
                verdict = {"AC": "OK", "WA": "WRONG_ANSWER", "PE": "PRESENTATION_ERROR", "JUDGE_ERROR": "CRASHED", "PARTIAL": "WRONG_ANSWER"}[verdict]
            return {"id": key, "expected": test["expected"], "actual": verdict, "matched": verdict == test["expected"], "comment": result["stderr"]}
        with self.work(component_case, tests.items()) as results:
            self.result["unit_tests"].extend(results)
        self.result["verified"] = bool(tests) and all(t["matched"] for t in self.result["unit_tests"])
        self.event("구성요소 자체 테스트 완료")

    def stress(self):
        StressSearch(self).run()

    def run(self):
        try:
            if self.job.kind == "package" and not self.job.options.get("verify", True):
                from .package_export import export_package
                self.result = export_package(self.job, self.publish, self.sandbox.check_cancelled)
                return self.result
            if self.job.kind == "prepare":
                self.prepare()
            elif self.job.kind in {"verify", "package"}:
                self.verify()
                if self.job.kind == "package":
                    from .packages import build_package
                    if self.job.options["verify"] and not self.result["verified"]:
                        raise SandboxError("풀이 기대 판정이 맞지 않아 검증된 패키지를 생성하지 않았습니다.")
                    overall_verified = self.result.get("verified", False)
                    for kind in ["validator-tests", "checker-tests"]:
                        if self.doc["validator_tests" if kind == "validator-tests" else "checker_tests"]:
                            self.unit_tests(kind)
                            if not self.result["verified"]:
                                raise SandboxError("구성요소 자체 테스트가 실패했습니다.")
                    self.result["verified"] = overall_verified
                    if self.job.options.get("export_format") == "linux":
                        from .linux_package import environment
                        self.event("재현용 Linux 실행 환경 확인")
                        self.result["replay_environment"] = environment(self.sandbox)
                        if self.result.get("solutions"):
                            from .test_matrix import analyze
                            self.result["test_matrix"] = analyze(self.doc, self.result)
                    self.result["package"] = build_package(self.job, self.result["testsets"], self.result)
                    self.event("패키지 생성 완료")
            elif self.job.kind in {"validator-tests", "checker-tests"}:
                self.unit_tests(self.job.kind)
            elif self.job.kind == "stress":
                self.stress()
            elif self.job.kind == "render":
                from .rendering import render_statements
                self.result["statements"] = render_statements(self.job, self.sandbox)
                self.event("지문 렌더링 완료")
            self.result["progress"] = 100
            return self.result
        finally:
            try:
                if self.result.get("solutions") and "test_matrix" not in self.result:
                    from .test_matrix import analyze
                    self.result["test_matrix"] = analyze(self.doc, self.result)
            finally:
                self.sandbox.close()
