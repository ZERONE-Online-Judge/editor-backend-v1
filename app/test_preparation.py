"""Prepare one test with dependency-specific successful-stage reuse."""
import shlex
from pathlib import Path
from .execution_limits import GENERATOR_CPU_MS, GENERATOR_MEMORY_MB, GENERATOR_OUTPUT_BYTES, SOLUTION_OUTPUT_BYTES
from .preparation_cache import content_hash
from .sandbox import SandboxError
from .job_errors import AssessmentFailure
from .storage import read_blob


def prepare_test(p, test, set_name, mode, frozen, main):
    cache = getattr(p, "preparation_cache", None)
    progress = getattr(p, "verification", None)
    def completed(stage):
        if progress:
            progress.preparation_step(set_name, test["index"], stage)
    stages = {}
    def step(stage, dependencies, execute):
        if cache:
            value, reused = cache.step(stage, dependencies, execute)
            stages[stage] = "reused" if reused else "executed"
        else:
            value = execute()
        if stage != "input":
            completed(stage)
        return value
    def program(name, solution=False):
        return cache.program(name, solution) if cache else None
    index = test["index"]
    p.sandbox.check_cancelled()
    p.event(f"테스트 자료 확인 · {set_name} #{index}")
    if frozen and (not test["input_sha"] or not test["answer_sha"]):
        raise SandboxError(f"{set_name} #{index}: 작성자가 입력·정답 생성 결과를 반영하고 커밋한 뒤 검수하세요.")
    if mode in {"validate", "answers"} and not test["input_sha"]:
        raise SandboxError(f"{set_name} #{index}: 저장된 입력이 없습니다. 먼저 입력을 생성하세요.")
    if frozen or mode in {"validate", "answers"} or not test["command"]:
        input_bytes = read_blob(test["input_sha"])
        stages["input"] = "stored"
    elif cache:
        tokens = shlex.split(test["command"])
        name = tokens[0] if tokens else ""
        if name not in p.doc["files"]:
            matches = [n for n in p.doc["files"] if Path(n).stem == name]
            if len(matches) != 1:
                raise SandboxError(f"생성기 파일을 찾을 수 없습니다: {name}")
            name = matches[0]
        value = step("input", {"program": program(name), "args": tokens[1:], "testset": set_name, "index": index,
                     "limits": [GENERATOR_CPU_MS, GENERATOR_MEMORY_MB, GENERATOR_OUTPUT_BYTES]},
                     lambda: {"blob_sha": p.blob(p.generator(test["command"]))})
        input_bytes = read_blob(value["blob_sha"])
    else:
        input_bytes = p.generator(test["command"])
    sha = content_hash(input_bytes)
    if p.doc["info"]["well_formed"] and (b"\r" in input_bytes or b"\x00" in input_bytes or (input_bytes and not input_bytes.endswith(b"\n"))):
        raise AssessmentFailure(f"{set_name} #{index}: LF 줄바꿈과 마지막 개행을 확인하세요.", 'input', 'input_format')
    completed("input")
    validators = [program(name) for name in [p.doc["validator"], *p.doc["extra_validators"]]] if cache else []
    def validate_input():
        valid, result = p.validator(input_bytes, set_name, test["group"])
        if not valid:
            if result.get('status') == 'ok' and result.get('exit_code', -1) >= 0:
                raise AssessmentFailure(f"{set_name} #{index}: 입력 조건 위반\n{result['stderr']}", 'input', 'input_rejected')
            raise SandboxError(f"{set_name} #{index}: Validator 실행 오류\n{result['stderr']}")
        return {"valid": True}
    step("validator", {"input": sha, "programs": validators, "testset": set_name, "group": test["group"]}, validate_input)
    if mode in {"inputs", "validate"}:
        answer_sha = test["answer_sha"] if mode == "validate" or sha == test["input_sha"] else None
        return ({**test, "input_sha": test["input_sha"] if mode == "validate" else None,
                 "answer_sha": answer_sha, "validator": "VALID", "main_cpu_ms": None, "main_memory_kb": None, "main_wall_ms": None,
                 "preparation": stages}, sha, input_bytes if mode == "inputs" else None, None)
    result = {"cpu_ms": None, "memory_kb": None, "wall_ms": None}
    # Include actual preparation limits and I/O, not tags or prose, in Main's key.
    info = p.doc["info"]
    main_dependencies = {"program": program(main, True), "input": sha,
                         "info": {key: info[key] for key in ("interactive", "input_file", "output_file", "time_limit_ms", "memory_limit_mb")},
                         "output_bytes": SOLUTION_OUTPUT_BYTES, "relaxed_limits": bool(getattr(p, "remote_judging", False)),
                         "native_reference": p.job.options.get("reference_output_protocol"),
                         "reference_revision": p.job.options.get("reference_revision"),
                         "language_resource_limits": p.doc.get("language_resource_limits", {})}
    if info["interactive"]:
        main_dependencies.update(interactor=program(p.doc["interactor"]), answer=test["answer_sha"])
    if frozen:
        answer = read_blob(test["answer_sha"])
        stages["answer"] = "stored"
        completed("answer")
    else:
        def generate_answer():
            nonlocal result
            verdict, result = p.run_solution(main, input_bytes, read_blob(test["answer_sha"]) if info["interactive"] and test["answer_sha"] else b"", preparation=True)
            if verdict != "AC":
                if verdict != 'JUDGE_ERROR':
                    raise AssessmentFailure(f"Main 풀이가 {set_name} #{index}에서 {verdict}입니다.\n{result['stderr']}", 'preparation', 'reference_failed')
                raise SandboxError(f"Main 풀이가 {set_name} #{index}에서 {verdict}입니다.\n{result['stderr']}")
            answer = (read_blob(test["answer_sha"]) if test["answer_sha"] else b"") if info["interactive"] else result["output"]
            return {"blob_sha": p.blob(answer)} if cache else {"answer": answer}
        value = step("answer", main_dependencies, generate_answer)
        answer = read_blob(value["blob_sha"]) if cache else value["answer"]
    checker_dependencies = {"program": program(p.doc["checker"]), "input": sha, "answer": content_hash(answer),
                            "points": test["points"], "points_enabled": info["points_enabled"], "checker_percent": info["checker_percent"]}
    def check_answer():
        check, checked = p.checker(input_bytes, answer, answer, test["points"])
        if check != "AC":
            if check != 'JUDGE_ERROR':
                raise AssessmentFailure(f"checker가 Main 출력을 거부했습니다.\n{checked['stderr']}", 'preparation', 'reference_rejected')
            raise SandboxError(f"Checker 실행 오류로 Main 출력을 확인하지 못했습니다.\n{checked['stderr']}")
        return {"accepted": True}
    step("checker", checker_dependencies, check_answer)
    row = {**test, "input_sha": test["input_sha"] if frozen else None, "answer_sha": test["answer_sha"] if frozen else None,
           "validator": "VALID", "main_cpu_ms": result["cpu_ms"], "main_memory_kb": result["memory_kb"], "main_wall_ms": result["wall_ms"],
           "preparation": stages}
    from .statement_samples import validate as validate_sample
    custom = test.get("statement_input") is not None or test.get("statement_output") is not None
    if test.get("sample") and custom:
        sample_dependencies = {"main": main_dependencies, "checker": checker_dependencies, "validators": validators,
                               "testset": set_name, "group": test["group"],
                               "sample": {key: test.get(key) for key in ("statement_input", "statement_output", "verify_statement_io")}}
        sample_status = step("sample", sample_dependencies,
                             lambda: {"status": validate_sample(p, test, set_name, input_bytes, answer, main)})["status"]
        row["statement_io_validation"] = sample_status
        p.event(f"지문 표시용 예제 · {set_name} #{index} · {'검증 통과' if sample_status == 'VALID' else '자동 검증 해제'}")
    return row, sha, None if frozen else input_bytes, None if frozen else answer
