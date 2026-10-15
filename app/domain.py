import copy
import hashlib
import json
import shlex
from fastapi import HTTPException
from sqlalchemy import select
from .db import Access, Blob, Problem, WorkingCopy, now
from .schema import Document
from .folder_access import effective_access
from .execution_limits import MAX_TESTS_PER_SET


def fail(code: str, message: str, status: int = 400, **details):
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **details})


def document_hash(doc):
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def authorize(db, problem_id, user, write=False, owner=False, allow_deleted=False):
    problem = db.get(Problem, problem_id)
    access = effective_access(db, problem_id, user.id)
    if not problem or not access or (problem.deleted and not allow_deleted):
        fail("not_found", "문제를 찾을 수 없습니다.", 404)
    if owner and access.role != "OWNER":
        fail("forbidden", "소유자만 이 작업을 할 수 있습니다.", 403)
    if write and access.role not in {"WRITE", "OWNER"}:
        fail("forbidden", "쓰기 권한이 필요합니다.", 403)
    return problem, access


def workspace(db, problem, user):
    row = db.get(WorkingCopy, (problem.id, user.id))
    if not row:
        row = WorkingCopy(problem_id=problem.id, user_id=user.id, base_revision=problem.revision,
                          base_document=copy.deepcopy(problem.document), document=copy.deepcopy(problem.document), version=0)
        db.add(row)
        db.commit()
    return row


def three_way_merge(base, ours, theirs):
    missing = object()
    conflicts = []
    def merge(b, o, t, path):
        if o == b:
            return copy.deepcopy(t) if t is not missing else missing
        if t == b or o == t:
            return copy.deepcopy(o) if o is not missing else missing
        if isinstance(b, dict) and isinstance(o, dict) and isinstance(t, dict):
            merged = {}
            for key in sorted(set(b) | set(o) | set(t)):
                value = merge(b.get(key, missing), o.get(key, missing), t.get(key, missing), path + [key])
                if value is not missing:
                    merged[key] = value
            return merged
        conflicts.append("/".join(path))
        return o
    result = merge(base, ours, theirs, [])
    return result, conflicts


def references(doc):
    result = set()
    def visit(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"sha256", "input_sha", "answer_sha", "output_sha"} and value:
                    result.add(value)
                else:
                    visit(value)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(doc)
    return result


def validate_document(db, problem_id, document):
    try:
        doc = Document.model_validate(document).model_dump()
    except ValueError as e:
        fail("invalid_document", "문제 자료를 확인해 주세요.", errors=json.loads(e.json()) if hasattr(e, "json") else str(e))
    allowed = set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem_id)))
    if references(doc) - allowed:
        fail("invalid_blob", "이 문제에 업로드하지 않은 파일을 사용할 수 없습니다.")
    return doc


def parse_script(source: str, occupied=()):
    from .generation_script import parse
    try:
        return parse(source, occupied)
    except ValueError as error:
        fail("invalid_script", f"생성 스크립트: {error}")


def expanded_tests(testset):
    saved = list(testset["tests"].values())
    if testset["script"].strip():
        saved = [test for test in saved if not test["command"]]
        generated = parse_script(testset["script"], [test["index"] for test in saved])
        if any(t.get("group") and (not testset["groups_enabled"] or t["group"] not in testset["groups"]) for t in generated):
            fail("invalid_script_group", "생성 스크립트의 그룹이 현재 테스트 세트에 없습니다.")
        previous = {(t["index"], t["command"]): t for t in testset["tests"].values() if t["command"]}
        fields = {"points", "description", "sample", "statement_input", "statement_output", "verify_statement_io"}
        saved += [{"input_sha": None, "answer_sha": None, "group": "", "points": 0,
                   "description": "", "sample": False,
                   **{k:v for k,v in previous.get((test["index"],test["command"]),{}).items() if k in fields}, **test} for test in generated]
    return sorted(saved, key=lambda test: test["index"])


def cautions(doc):
    warnings = []
    def add(code, label, section, blocking=False):
        warnings.append({"code": code, "message": label, "section": section, "blocking": blocking})
    if not doc["statements"] or not (doc.get("common_statement", "").strip() or any(s.get("legend", "").strip() for s in doc["statements"].values())):
        add("NO_STATEMENT", "문제 지문을 작성하세요.", "statements", True)
    if not doc["checker"]:
        add("NO_CHECKER", "checker를 선택하세요.", "checker", True)
    if not doc["validator"]:
        add("NO_VALIDATOR", "입력 조건을 확인할 validator를 선택하세요.", "validator", True)
    elif not doc["validator_tests"]:
        add("NO_VALIDATOR_TESTS", "유효·무효 입력으로 validator 자체도 테스트하세요.", "validator")
    if doc["checker"] and not doc["checker"].startswith("std::") and not doc["checker_tests"]:
        add("NO_CHECKER_TESTS", "사용자 checker의 정상·오답 출력 테스트를 추가하세요.", "checker")
    if doc["info"]["points_enabled"] and not any(t["points"] > 0 for ts in doc["testsets"].values() for t in ts["tests"].values()):
        add("NO_POINTS", "부분 점수를 사용할 테스트에 점수를 지정하세요.", "tests", True)
    if not any(s["tag"] == "MA" for s in doc["solutions"].values()):
        add("NO_MAIN", "기대 출력을 생성할 Main 풀이를 지정하세요.", "solutions", True)
    if not any(ts["tests"] or ts["script"].strip() for ts in doc["testsets"].values()):
        add("NO_TESTS", "직접 입력이나 생성 스크립트로 테스트를 추가하세요.", "tests", True)
    wrong_tags = {"WA", "TL", "ML", "RE", "RJ", "PE", "TM"}
    def has_wrong_tag(source):
        tags = [source["tag"], *source.get("extra_tags", {}).values()]
        tags.extend(tag for groups in source.get("group_tags", {}).values() for tag in groups.values())
        return source["tag"] != "NR" and bool(wrong_tags.intersection(tags))
    if not any(has_wrong_tag(source) for source in doc["solutions"].values()):
        add("NO_WRONG_SOLUTIONS", "의도한 오답 풀이를 추가해 테스트의 검출력을 확인하세요.", "solutions")
    if doc["info"]["interactive"] and not doc["interactor"]:
        add("NO_INTERACTOR", "인터랙티브 문제의 interactor를 선택하세요.", "interactor", True)
    return warnings
