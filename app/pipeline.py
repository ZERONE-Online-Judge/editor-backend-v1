import copy
import hashlib
import shlex
import math
import re
import time
import threading
from pathlib import Path
from .db import SessionLocal, User
from .domain import expanded_tests
from .sandbox import Sandbox, SandboxError, VENDOR
from .storage import put_blob, read_blob
from .scoring import score_results
from .execution_limits import MAX_GENERATOR_ARGUMENTS, GENERATOR_CPU_MS, GENERATOR_MEMORY_MB, GENERATOR_OUTPUT_BYTES
from .stress import DEFAULT_VERDICTS, generator_command, stress_configuration


class StressDurationReached(RuntimeError):
    pass


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
        self.sandbox = Sandbox(check_cancelled, remote_context=context)
        self.result = {"summary": "자료를 준비하고 있습니다.", "progress": 0, "events": [], "testsets": {}, "solutions": [], "unit_tests": []}
        if job.options.get("scope"):
            self.result["scope"] = copy.deepcopy(job.options["scope"])
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
        with SessionLocal() as db:
            value = put_blob(db, self.job.problem_id, content)
            db.commit()
            return value["sha256"]

    def program(self, name, solution=False):
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
        main = self.main()
        frozen = self.job.kind == "verify" and self.job.options.get("snapshot") == "committed"
        all_sets = self.job.kind == "package" or self.job.options.get("all_testsets", False)
        selected = list(self.doc["testsets"]) if all_sets else [self.job.options.get("testset", "tests")]
        self.result["processed_testsets"] = selected
        for set_name in selected:
            testset = self.doc["testsets"][set_name]
            saved = expanded_tests(testset)
            prepared = []
            seen = set()
            for test in sorted(saved, key=lambda t: t["index"]):
                if self.job.kind == "verify" and self.job.options.get("test_indices") and test["index"] not in self.job.options["test_indices"]:
                    continue
                self.sandbox.check_cancelled()
                index = test["index"]
                self.event(f"테스트 준비 · {set_name} #{index}")
                if frozen and (not test["input_sha"] or not test["answer_sha"]):
                    raise SandboxError(f"{set_name} #{index}: 작성자가 입력·정답 생성 결과를 반영하고 커밋한 뒤 검수하세요.")
                input_bytes = read_blob(test["input_sha"]) if frozen or not test["command"] else self.generator(test["command"])
                sha = hashlib.sha256(input_bytes).hexdigest()
                if sha in seen and not self.doc["info"]["skip_duplicate_validation"]:
                    raise SandboxError(f"{set_name} #{index}: 중복된 입력입니다.")
                seen.add(sha)
                if self.doc["info"]["well_formed"] and (b"\r" in input_bytes or b"\x00" in input_bytes or (input_bytes and not input_bytes.endswith(b"\n"))):
                    raise SandboxError(f"{set_name} #{index}: LF 줄바꿈과 마지막 개행을 확인하세요.")
                valid, validator = self.validator(input_bytes, set_name, test["group"])
                if not valid:
                    raise SandboxError(f"{set_name} #{index}: 입력 조건 위반\n{validator['stderr']}")
                if frozen:
                    answer = read_blob(test["answer_sha"])
                    result = {"cpu_ms": None, "memory_kb": None, "wall_ms": None}
                else:
                    verdict, result = self.run_solution(main, input_bytes, read_blob(test["answer_sha"]) if self.doc["info"]["interactive"] and test["answer_sha"] else b"", preparation=True)
                    if verdict != "AC":
                        raise SandboxError(f"Main 풀이가 {set_name} #{index}에서 {verdict}입니다.\n{result['stderr']}")
                    # Interactive answer files are interactor resources, not a
                    # contestant transcript. Preserve supplied reference data.
                    answer = (read_blob(test["answer_sha"]) if test["answer_sha"] else b"") if self.doc["info"]["interactive"] else result["output"]
                check, checked = self.checker(input_bytes, answer, answer, test["points"])
                if check != "AC":
                    raise SandboxError(f"checker가 Main 출력을 거부했습니다.\n{checked['stderr']}")
                row = {**test, "input_sha": test["input_sha"] if frozen else self.blob(input_bytes), "answer_sha": test["answer_sha"] if frozen else self.blob(answer), "validator": "VALID", "main_cpu_ms": result["cpu_ms"], "main_memory_kb": result["memory_kb"], "main_wall_ms": result["wall_ms"]}
                prepared.append(row)
            self.result["testsets"][set_name] = prepared
        self.event(f"{'커밋된 입력·기대 출력 확인 완료' if frozen else '입력 검증과 Main 기대 출력 생성 완료'} · {', '.join(selected)}")

    @staticmethod
    def expected_matches(tag, verdicts):
        failures = set(verdicts) - {"AC"}
        if tag in {"MA", "OK"}:
            return bool(verdicts) and not failures
        if tag == "NR":
            return True
        if "JUDGE_ERROR" in failures or "CE" in failures:
            return False
        if tag == "RJ":
            return bool(failures)
        if tag == "TO":
            return bool(verdicts) and failures <= {"TLE"}
        if tag == "TM":
            return bool(failures) and failures <= {"TLE", "MLE"}
        required = {"WA": "WA", "TL": "TLE", "ML": "MLE", "RE": "RE", "PE": "PE"}.get(tag)
        return required in failures

    def verify(self):
        self.prepare()
        if self.remote_judging:
            from .judge_bridge import verify
            verify(self)
            return
        selected = list(self.doc["solutions"]) if self.job.kind == "package" else self.job.options["solutions"] or list(self.doc["solutions"])
        for name in selected:
            source = self.doc["solutions"][name]
            rows = []
            verdicts = []
            if source["tag"] == "NR":
                self.result["solutions"].append({"name": name, "tag": "NR", "matched": True, "cases": [], "skipped": True})
                continue
            for set_name, tests in self.result["testsets"].items():
                if self.job.kind != "package" and set_name != self.job.options["testset"]:
                    continue
                for test in tests:
                    if self.job.kind != "package" and self.job.options["test_indices"] and test["index"] not in self.job.options["test_indices"]:
                        continue
                    self.event(f"실제 실행 · {name} / {set_name} #{test['index']}")
                    inp, ans = read_blob(test["input_sha"]), read_blob(test["answer_sha"])
                    verdict, result = self.run_solution(name, inp, ans)
                    checked = {"score": 0}
                    comment = result.get("stderr", "")
                    if verdict == "AC":
                        verdict, checked = self.checker(inp, result["output"], ans, test["points"])
                        comment = checked["stderr"]
                    verdicts.append(verdict)
                    rows.append({"testset": set_name, "index": test["index"], "verdict": verdict, "cpu_ms": result["cpu_ms"], "wall_ms": result["wall_ms"], "memory_kb": result["memory_kb"], "comment": comment[:2048], "score": checked.get("score", 0) if verdict in {"AC", "PARTIAL", "WA", "PE"} else 0, "group": test["group"], "max_score": test["points"],
                                 "input_sha": test["input_sha"], "answer_sha": test["answer_sha"], "actual_sha": self.blob(result["output"])})
            tag_matches = self.expected_matches(source["tag"], verdicts)
            for set_name, expected_tag in source.get("extra_tags", {}).items():
                scoped = [r["verdict"] for r in rows if r["testset"] == set_name or next((t["group"] for t in self.result["testsets"].get(r["testset"], []) if t["index"] == r["index"]), "") == set_name]
                if scoped:
                    tag_matches = tag_matches and self.expected_matches(expected_tag, scoped)
            score_scope = {name: expanded_tests(self.doc["testsets"][name]) for name in self.result["testsets"]}
            self.result["solutions"].append({"name": name, "tag": source["tag"], "matched": tag_matches, "cases": rows, "skipped": False, **score_results(self.doc, score_scope, rows)})
            self.publish(copy.deepcopy(self.result))
        mismatches = [s["name"] for s in self.result["solutions"] if not s["matched"]]
        self.result["verified"] = not mismatches
        self.result["mismatches"] = mismatches
        self.event("풀이 기대 판정 확인 완료" if not mismatches else "기대 판정과 다른 풀이가 있습니다.")

    def unit_tests(self, kind):
        tests = self.doc["validator_tests" if kind == "validator-tests" else "checker_tests"]
        for key, test in tests.items():
            self.event(f"구성요소 테스트 · #{key}")
            inp = read_blob(test["input_sha"])
            if kind == "validator-tests":
                valid, result = self.validator(inp)
                verdict = "VALID" if valid else "INVALID" if result["status"] == "ok" and result["exit_code"] >= 0 else "CRASHED"
            else:
                verdict, result = self.checker(inp, read_blob(test["output_sha"]), read_blob(test["answer_sha"]))
                verdict = {"AC": "OK", "WA": "WRONG_ANSWER", "PE": "PRESENTATION_ERROR", "JUDGE_ERROR": "CRASHED", "PARTIAL": "WRONG_ANSWER"}[verdict]
            self.result["unit_tests"].append({"id": key, "expected": test["expected"], "actual": verdict, "matched": verdict == test["expected"], "comment": result["stderr"]})
        self.result["verified"] = bool(tests) and all(t["matched"] for t in self.result["unit_tests"])
        self.event("구성요소 자체 테스트 완료")

    def stress(self):
        options = self.job.options
        reference = options["reference"] or self.main()
        candidates = list(dict.fromkeys(options.get("candidates") or ([options.get("candidate")] if options.get("candidate") else [])))
        if not candidates or not options["generator"].strip():
            raise SandboxError("생성기 명령과 비교할 풀이를 선택하세요.")
        started = time.monotonic()
        duration = options.get("duration_seconds")
        deadline = started + duration if duration else None
        original_check = self.sandbox.check_cancelled
        stats = {"iterations_started": 0, "iterations_completed": 0, "elapsed_seconds": 0,
                 "duplicates_skipped": 0, "counterexample_count": 0, "stop_reason": None,
                 "last_seed": None, "last_command": None}
        summaries = {name: {"name": name, "tag": self.doc["solutions"][name]["tag"], "tested": 0,
                            "verdicts": {}, "first_counterexample_id": None} for name in candidates}
        self.result.update(counterexample=None, counterexamples=[], stress=stats, candidate_summaries=list(summaries.values()),
                           stress_configuration=stress_configuration(self.doc, options))
        seen = set()
        collected_verdicts = set(options.get("counterexample_verdicts") or DEFAULT_VERDICTS)
        def check():
            original_check()
            stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
            if deadline is not None and time.monotonic() >= deadline:
                raise StressDurationReached()
            if time.monotonic() - self.last_publish >= 1:
                self.publish(copy.deepcopy(self.result))
                self.last_publish = time.monotonic()
        self.sandbox.check_cancelled = check
        try:
            for i in range(1, options["iterations"] + 1):
                check()
                stats["iterations_started"] = i
                command = generator_command(options["generator"], i)
                stats.update(last_seed=i, last_command=command)
                self.event(f"스트레스 테스트 · {i} / {options['iterations']} · 시드 {i} · 비교 풀이 {len(candidates)}개",
                           seed=i)
                inp = self.generator(command)
                input_sha = hashlib.sha256(inp).hexdigest()
                if input_sha in seen:
                    stats["duplicates_skipped"] += 1
                    stats["iterations_completed"] = i
                    continue
                valid, validation = self.validator(inp, options["testset"])
                if not valid:
                    raise SandboxError("생성한 반례 입력이 유효하지 않습니다.\n" + validation["stderr"])
                seen.add(input_sha)
                verdict, correct = self.run_solution(reference, inp)
                if verdict != "AC":
                    raise SandboxError(f"기준 풀이가 {verdict}입니다.")
                example = None
                for candidate in candidates:
                    check()
                    self.event(f"비교 풀이 실행 · {candidate} / 반복 {i}")
                    verdict, wrong = self.run_solution(candidate, inp, correct["output"])
                    checked = {"stderr": wrong["stderr"]}
                    if verdict == "AC":
                        verdict, checked = self.checker(inp, wrong["output"], correct["output"])
                    if verdict == "JUDGE_ERROR":
                        raise SandboxError("checker 또는 격리 실행 환경의 오류입니다.\n" + checked["stderr"])
                    summary = summaries[candidate]
                    summary["tested"] += 1
                    summary["verdicts"][verdict] = summary["verdicts"].get(verdict, 0) + 1
                    if verdict in collected_verdicts:
                        if example is None:
                            example = {"id": input_sha[:32], "iteration": i, "seed": i, "command": command,
                                       "input_sha": self.blob(inp), "answer_sha": self.blob(correct["output"]), "findings": []}
                            self.result["counterexamples"].append(example)
                            stats["counterexample_count"] += 1
                        finding = {"solution": candidate, "tag": summary["tag"], "verdict": verdict,
                                   "actual_sha": self.blob(wrong["output"]), "comment": checked["stderr"],
                                   "cpu_ms": wrong.get("cpu_ms"), "memory_kb": wrong.get("memory_kb")}
                        example["findings"].append(finding)
                        if summary["first_counterexample_id"] is None:
                            summary["first_counterexample_id"] = example["id"]
                        if self.result["counterexample"] is None:
                            self.result["counterexample"] = {**example, **finding}
                        self.event(f"유효한 반례 발견 · {candidate} · {verdict}")
                        self.publish(copy.deepcopy(self.result))
                stats["iterations_completed"] = i
                if example and options.get("stop_on_first", True):
                    stats["stop_reason"] = "first_counterexample"
                    break
                if stats["counterexample_count"] >= options.get("counterexample_limit", 1):
                    stats["stop_reason"] = "counterexample_target"
                    break
            else:
                stats["stop_reason"] = "iterations"
        except StressDurationReached:
            stats["stop_reason"] = "duration"
        finally:
            self.sandbox.check_cancelled = original_check
            stats["elapsed_seconds"] = round(time.monotonic() - started, 2)
            self.result["iterations"] = stats["iterations_completed"]
        reason = {"duration": "실행 시간 한도", "iterations": "반복 횟수 한도", "first_counterexample": "첫 반례 발견", "counterexample_target": "목표 반례 수"}.get(stats["stop_reason"], "실행 종료")
        self.event(f"스트레스 테스트 완료 · {reason} · {stats['iterations_completed']}회 · 유효한 반례 {stats['counterexample_count']}개")

    def run(self):
        try:
            if self.job.kind == "prepare":
                self.prepare()
            elif self.job.kind in {"verify", "package"}:
                if self.job.kind == "package" and not self.job.options["verify"]:
                    self.prepare()
                    self.result["verified"] = False
                else:
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
                    self.result["package"] = build_package(self.job, self.result["testsets"])
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
            self.sandbox.close()
