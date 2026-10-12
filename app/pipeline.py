import copy
import hashlib
import shlex
import math
import re
import time
import subprocess
from pathlib import Path
from .db import SessionLocal
from .domain import parse_script
from .sandbox import Sandbox, SandboxError, VENDOR
from .storage import put_blob, read_blob
from .scoring import score_results


class Pipeline:
    def __init__(self, job, publish, check_cancelled):
        self.job = job
        self.doc = job.document
        self.publish = publish
        self.sandbox = Sandbox(check_cancelled)
        self.result = {"summary": "자료를 준비하고 있습니다.", "progress": 0, "events": [], "testsets": {}, "solutions": [], "unit_tests": []}
        self.programs = {}
        self.counter = 0
        self.last_publish = 0
        inspected = subprocess.run(["docker", "image", "inspect", self.sandbox.settings.runner_image, "--format", "{{.Id}}"], capture_output=True, text=True, timeout=10)
        self.result["runtime"] = {"image": self.sandbox.settings.runner_image, "image_id": inspected.stdout.strip(), "cpu_measurement": "process-and-descendants"}

    def event(self, label, **extra):
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
        if not tokens or len(tokens) > 65:
            raise SandboxError("생성기와 최대 64개 인자를 입력하세요.")
        name = tokens[0]
        if name not in self.doc["files"]:
            matches = [n for n in self.doc["files"] if Path(n).stem == name]
            if len(matches) != 1:
                raise SandboxError(f"생성기 파일을 찾을 수 없습니다: {name}")
            name = matches[0]
        program = self.program(name)
        result = self.sandbox.run(program, args=tokens[1:], cpu_ms=10000, memory_mb=512, output_bytes=16 * 1024 * 1024)
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

    def run_solution(self, name, input_bytes, answer=b""):
        info = self.doc["info"]
        program = self.program(name, True)
        if info["interactive"]:
            actor = self.program(self.doc["interactor"])
            result = self.sandbox.interactive(program, actor, input_bytes, answer, cpu_ms=info["time_limit_ms"], memory_mb=info["memory_limit_mb"])
        else:
            result = self.sandbox.run(program, input_bytes, cpu_ms=info["time_limit_ms"], memory_mb=info["memory_limit_mb"], input_file=info["input_file"], output_file=info["output_file"])
        status = result["status"]
        if status != "ok":
            verdict = {"time-limit": "TLE", "memory-limit": "MLE", "output-limit": "OLE", "wall-limit": "IDLENESS_LIMIT" if info["interactive"] else "JUDGE_ERROR"}.get(status, "JUDGE_ERROR")
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
        for set_name, testset in self.doc["testsets"].items():
            saved = list(testset["tests"].values())
            if testset["script"].strip():
                saved = [test for test in saved if not test["command"]]
                generated = parse_script(testset["script"], [test["index"] for test in saved])
                saved += [{"input_sha": None, "answer_sha": None, "group": "", "points": 0, "description": "", "sample": False, **test} for test in generated]
            prepared = []
            seen = set()
            for test in sorted(saved, key=lambda t: t["index"]):
                self.sandbox.check_cancelled()
                index = test["index"]
                self.event(f"테스트 준비 · {set_name} #{index}")
                input_bytes = self.generator(test["command"]) if test["command"] else read_blob(test["input_sha"])
                sha = hashlib.sha256(input_bytes).hexdigest()
                if sha in seen and not self.doc["info"]["skip_duplicate_validation"]:
                    raise SandboxError(f"{set_name} #{index}: 중복된 입력입니다.")
                seen.add(sha)
                if self.doc["info"]["well_formed"] and (b"\r" in input_bytes or b"\x00" in input_bytes or (input_bytes and not input_bytes.endswith(b"\n"))):
                    raise SandboxError(f"{set_name} #{index}: LF 줄바꿈과 마지막 개행을 확인하세요.")
                valid, validator = self.validator(input_bytes, set_name, test["group"])
                if not valid:
                    raise SandboxError(f"{set_name} #{index}: 입력 조건 위반\n{validator['stderr']}")
                verdict, result = self.run_solution(main, input_bytes, read_blob(test["answer_sha"]) if self.doc["info"]["interactive"] and test["answer_sha"] else b"")
                if verdict != "AC":
                    raise SandboxError(f"Main 풀이가 {set_name} #{index}에서 {verdict}입니다.\n{result['stderr']}")
                answer = result["output"]
                check, checked = self.checker(input_bytes, answer, answer, test["points"])
                if check != "AC":
                    raise SandboxError(f"checker가 Main 출력을 거부했습니다.\n{checked['stderr']}")
                row = {**test, "input_sha": self.blob(input_bytes), "answer_sha": self.blob(answer), "validator": "VALID", "main_cpu_ms": result["cpu_ms"], "main_memory_kb": result["memory_kb"], "main_wall_ms": result["wall_ms"]}
                prepared.append(row)
            self.result["testsets"][set_name] = prepared
        self.event("입력 검증과 Main 기대 출력 생성이 완료되었습니다.")

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
                tag_matches = tag_matches and self.expected_matches(expected_tag, scoped)
            self.result["solutions"].append({"name": name, "tag": source["tag"], "matched": tag_matches, "cases": rows, "skipped": False, **score_results(self.doc, self.result["testsets"], rows)})
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
        candidate = options["candidate"]
        if not candidate or not options["generator"].strip():
            raise SandboxError("생성기 명령과 비교할 풀이를 선택하세요.")
        self.result["counterexample"] = None
        for i in range(1, options["iterations"] + 1):
            self.event(f"스트레스 테스트 · {i} / {options['iterations']}")
            command = options["generator"].replace("{seed}", str(i))
            if "{seed}" not in options["generator"]:
                command += " " + str(i)
            inp = self.generator(command)
            valid, validation = self.validator(inp, options["testset"])
            if not valid:
                raise SandboxError("생성한 반례 입력이 유효하지 않습니다.\n" + validation["stderr"])
            verdict, correct = self.run_solution(reference, inp)
            if verdict != "AC":
                raise SandboxError(f"기준 풀이가 {verdict}입니다.")
            verdict, wrong = self.run_solution(candidate, inp, correct["output"])
            checked = {"stderr": wrong["stderr"]}
            if verdict == "AC":
                verdict, checked = self.checker(inp, wrong["output"], correct["output"])
            if verdict == "JUDGE_ERROR":
                raise SandboxError("checker 또는 격리 실행 환경의 오류입니다.\n" + checked["stderr"])
            if verdict != "AC":
                self.result["counterexample"] = {"iteration": i, "command": command, "verdict": verdict, "input_sha": self.blob(inp), "answer_sha": self.blob(correct["output"]), "actual_sha": self.blob(wrong["output"]), "comment": checked["stderr"]}
                self.event(f"유효한 반례 발견 · {verdict}")
                return
        self.result["iterations"] = options["iterations"]
        self.event("설정한 반복 횟수에서는 반례를 찾지 못했습니다.")

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
