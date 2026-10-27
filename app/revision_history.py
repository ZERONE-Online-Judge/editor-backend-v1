"""Shared commit histories and bounded previews of committed documents only."""
import codecs
import difflib
import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from .auth import current_user
from .db import Blob, Folder, Problem, Revision, SharedPlacement, User, session_dependency
from .domain import authorize, fail
from .folders import owned_folder
from .pagination import page_rows
from .storage import blob_path

router = APIRouter(prefix="/api", tags=["revision history"])
PREVIEW_BYTES = 32768
PREVIEW_LINES = 400
PREVIEW_FILES = 3
FIELDS = {"name": "이름", "time_limit_ms": "시간 제한 (ms)", "memory_limit_mb": "메모리 제한 (MiB)",
          "legend": "지문", "input": "입력 형식", "output": "출력 형식", "notes": "예제 설명",
          "tutorial": "해설", "scoring": "점수", "interaction": "상호작용", "script": "생성 스크립트",
          "groups": "테스트 그룹", "groups_enabled": "그룹 사용", "checker": "Checker",
          "validator": "Validator", "interactor": "Interactor", "extra_validators": "추가 Validator",
          "common_statement": "공통 설명", "common_tutorial": "공통 해설",
          "translation_source": "번역 기준 원문", "translation_reviews": "번역 검토 상태",
          "language_resource_limits": "언어별 시간·메모리 제한"}


def changes(before, after):
    """Keep a file or test together rather than exposing each hash as a change."""
    result = []
    missing = object()

    def add(path, label, a, b):
        if a == b:
            return
        result.append({"path": path, "label": label,
                       "kind": "added" if a is missing else "removed" if b is missing else "changed",
                       "before": None if a is missing else a, "after": None if b is missing else b})

    def fields(path, a, b, label=""):
        for key in sorted(set(a) | set(b)):
            add(f"{path}/{key}", f"{label} · {FIELDS.get(key, key)}" if label else FIELDS.get(key, key),
                a.get(key, missing), b.get(key, missing))

    for section in sorted(set(before) | set(after)):
        a, b = before.get(section, {}), after.get(section, {})
        if a == b:
            continue
        if section == "info":
            fields(section, a, b)
        elif section in {"files", "solutions", "materials", "validator_tests", "checker_tests"}:
            for name in sorted(set(a) | set(b)):
                add(f"{section}/{name}", name, a.get(name, missing), b.get(name, missing))
        elif section == "statements":
            for language in sorted(set(a) | set(b)):
                name = {"korean": "한국어", "english": "영어"}.get(language, language)
                fields(f"statements/{language}", a.get(language, {}), b.get(language, {}), name)
        elif section == "testsets":
            for name in sorted(set(a) | set(b)):
                aa, bb = a.get(name, {}), b.get(name, {})
                fields(f"testsets/{name}", {k: v for k, v in aa.items() if k != "tests"},
                       {k: v for k, v in bb.items() if k != "tests"}, name)
                tests_a = {t["index"]: t for t in aa.get("tests", {}).values()}
                tests_b = {t["index"]: t for t in bb.get("tests", {}).values()}
                for index in sorted(set(tests_a) | set(tests_b)):
                    add(f"testsets/{name}/tests/{index}", f"{name} #{index}",
                        tests_a.get(index, missing), tests_b.get(index, missing))
        else:
            add(section, FIELDS.get(section, section), before.get(section, missing), after.get(section, missing))
    return result


def change_summary(before, after):
    rows = changes(before, after)
    return {"total": len(rows), "items": [{k: row[k] for k in ("path", "label", "kind")} for row in rows[:6]]}


def preview_text(db, problem_id, value, key=None):
    if key:
        sha = value.get(key) if isinstance(value, dict) else None
        if not sha:
            return "", False, False, False
        if not db.get(Blob, (problem_id, sha)):
            return "", False, False, True
        try:
            with blob_path(sha).open("rb") as stream:
                raw = stream.read(PREVIEW_BYTES + 1)
        except OSError:
            return "", False, False, True
        # A partial multibyte character at the boundary is not a binary file.
        try:
            text = codecs.getincrementaldecoder("utf-8")().decode(raw[:PREVIEW_BYTES], final=len(raw) <= PREVIEW_BYTES)
            binary = b"\0" in raw
        except UnicodeDecodeError:
            text, binary = "", True
        return text, len(raw) > PREVIEW_BYTES, binary, False
    text = "" if value is None else value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return text[:PREVIEW_BYTES], len(text) > PREVIEW_BYTES, False, False


def compact_diff(db, problem_id, change):
    a, b = change["before"], change["after"]
    contents = []
    for key, label in (("sha256", "파일 내용"), ("input_sha", "입력"), ("answer_sha", "정답"), ("output_sha", "출력")):
        aa, bb = (a or {}).get(key) if isinstance(a, dict) else None, (b or {}).get(key) if isinstance(b, dict) else None
        if aa != bb:
            contents.append((key, label))
    key, content_label = contents[0] if contents else (None, None)
    old, old_cut, old_binary, old_missing = preview_text(db, problem_id, a, key)
    new, new_cut, new_binary, new_missing = preview_text(db, problem_id, b, key)
    old_lines, new_lines = old.splitlines(), new.splitlines()
    cut = old_cut or new_cut or len(old_lines) > PREVIEW_LINES or len(new_lines) > PREVIEW_LINES
    binary, unavailable = old_binary or new_binary, old_missing or new_missing
    lines = []
    if not binary and not unavailable:
        diff = difflib.unified_diff(old_lines[:PREVIEW_LINES], new_lines[:PREVIEW_LINES], n=1, lineterm="")
        next(diff, None)
        next(diff, None)
        for line in diff:
            if len(lines) == 9:
                cut = True
                break
            lines.append({"kind": "hunk" if line.startswith("@@") else "add" if line.startswith("+") else "remove" if line.startswith("-") else "same",
                          "text": line[:240]})
            cut |= len(line) > 240
        if not lines and old != new and not cut:
            def endings(text):
                crlf = text.count("\r\n")
                return f"줄바꿈: CRLF {crlf}개 · LF {text.count(chr(10)) - crlf}개 · CR {text.count(chr(13)) - crlf}개 · 마지막 개행 {'있음' if text.endswith((chr(10), chr(13))) else '없음'}"
            lines = [{"kind": "remove", "text": "- " + endings(old)},
                     {"kind": "add", "text": "+ " + endings(new)}]
    return {k: change[k] for k in ("path", "label", "kind")} | {
        "content_label": content_label, "lines": lines, "truncated": cut,
        "binary": binary, "unavailable": unavailable,
        "additional_contents": max(0, len(contents) - 1)}


@router.get("/problems/{problem_id}/revision-preview")
def revision_preview(problem_id: str, from_revision: int = Query(ge=-1), to_revision: int | None = Query(default=None, ge=0),
                     user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    problem, _ = authorize(db, problem_id, user)
    target = problem.revision if to_revision is None else to_revision
    if from_revision > target:
        fail("invalid_revision_range", "비교할 리비전의 순서를 확인하세요.")
    if target > problem.revision:
        fail("not_found", "리비전을 찾을 수 없습니다.", 404)
    after = db.get(Revision, (problem_id, target))
    before = db.get(Revision, (problem_id, from_revision)) if from_revision >= 0 else None
    if not after or (from_revision >= 0 and not before):
        fail("not_found", "리비전을 찾을 수 없습니다.", 404)
    commits_query = (select(Revision.number, Revision.message, Revision.minor, Revision.created_at, User.display_name.label("author"))
                     .join(User, User.id == Revision.author_id)
                     .where(Revision.problem_id == problem_id, Revision.number > from_revision, Revision.number <= target)
                     .order_by(Revision.number.desc()))
    commits, meta = page_rows(db, commits_query, 1, 5)
    rows = changes(before.document if before else {}, after.document)
    # Prefer actual files/code in a short notice; all changes remain available in
    # the existing full revision viewer, including changes outside these bounds.
    rows.sort(key=lambda row: (not row["path"].startswith(("files/", "solutions/", "statements/", "testsets/")), row["path"]))
    return {"from_revision": from_revision, "to_revision": target,
            "commits": {"total": meta["total"], "items": [dict(row._mapping) for row in commits]},
            "changes": {"total": len(rows), "items": [compact_diff(db, problem_id, row) for row in rows[:PREVIEW_FILES]]}}


@router.get("/folders/{folder_id}/revisions")
def folder_revisions(folder_id: str, page: int = Query(default=1, ge=1, le=1000000), page_size: int = Query(default=10, ge=1, le=10),
                     user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    folder = owned_folder(db, user, folder_id, write=False)
    if not folder.workspace_id:
        fail("folder_not_found", "팀 폴더를 찾을 수 없습니다.", 404)
    tree = {row.id: row for row in db.scalars(select(Folder).where(Folder.workspace_id == folder.workspace_id))}
    ids, frontier = {folder_id}, {folder_id}
    while frontier:
        frontier = {row.id for row in tree.values() if row.parent_id in frontier} - ids
        ids |= frontier
    previous = aliased(Revision)
    query = (select(Revision, previous.document.label("before"), Problem.slug, Problem.document["info"]["name"].as_string().label("problem_name"),
                    SharedPlacement.folder_id, User.display_name.label("author"))
             .join(Problem, Problem.id == Revision.problem_id)
             .join(SharedPlacement, SharedPlacement.problem_id == Problem.id)
             .join(User, User.id == Revision.author_id)
             .outerjoin(previous, (previous.problem_id == Revision.problem_id) & (previous.number == Revision.number - 1))
             .where(SharedPlacement.folder_id.in_(ids), Problem.deleted.is_(False))
             .order_by(Revision.created_at.desc(), Revision.problem_id, Revision.number.desc()))
    rows, metadata = page_rows(db, query, page, page_size)

    def path(ident):
        parts = []
        seen = set()
        while ident in tree and ident not in seen:
            seen.add(ident)
            node = tree[ident]
            parts.insert(0, node.name)
            ident = node.parent_id
        return parts

    return {**metadata, "items": [{"problem_id": rev.problem_id, "problem_name": name, "slug": slug,
            "folder_path": path(ident), "number": rev.number, "message": rev.message, "minor": rev.minor,
            "author": author, "created_at": rev.created_at, "changes": change_summary(before or {}, rev.document)}
            for rev, before, slug, name, ident, author in rows]}
