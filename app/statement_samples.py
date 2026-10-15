"""Display-only samples never replace judging inputs or reference answers."""
from .storage import read_blob
from .sandbox import SandboxError

FIELDS = {"statement_input", "statement_output", "verify_statement_io"}


def display(test, field):
    custom = test.get("statement_input" if field == "input" else "statement_output")
    if custom is not None: return custom
    sha = test.get("input_sha" if field == "input" else "answer_sha")
    return read_blob(sha).decode("utf-8", "replace") if sha else ""


def validate(pipeline, test, set_name, original_input, original_answer, main):
    custom = test.get("statement_input") is not None or test.get("statement_output") is not None
    if not test.get("sample") or not custom: return None
    if not test.get("verify_statement_io", True): return "NOT_REQUESTED"
    if pipeline.doc["info"]["interactive"]:
        raise SandboxError("인터랙티브 대화 예제는 자동 검증할 수 없습니다. 표시용 검증을 해제하고 대화 규약에 맞는지 직접 검수하세요.")
    inp = test["statement_input"].encode() if test.get("statement_input") is not None else original_input
    out = test["statement_output"].encode() if test.get("statement_output") is not None else original_answer
    valid, result = pipeline.validator(inp, set_name, test.get("group", ""))
    if not valid: raise SandboxError(f"#{test['index']} 지문 표시용 입력이 Validator 조건을 위반했습니다.\n{result['stderr']}")
    verdict, reference = pipeline.run_solution(main, inp, preparation=True)
    if verdict != "AC": raise SandboxError(f"지문 표시용 입력에서 Main 판정이 {verdict}입니다.")
    verdict, checked = pipeline.checker(inp, out, reference["output"], test.get("points", 0))
    if verdict != "AC": raise SandboxError(f"#{test['index']} 지문 표시용 출력이 Checker를 통과하지 못했습니다: {verdict}\n{checked['stderr']}")
    return "VALID"
