"""Canonical LF text for test data, with explicit raw component-test overrides."""
import copy
import tempfile

from sqlalchemy import select

from .blob_text_formats import profile, _write_lf
from .db import Blob
from .domain import fail
from .storage import blob_path, put_blob_file


def normalize_test_bytes(content: bytes) -> bytes:
    if b"\r" not in content or b"\0" in content:
        return content
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def normalize_test_document(db, problem_id, doc, areas=None):
    document = copy.deepcopy(doc)
    selected = {"testsets", "validator_tests", "checker_tests"} if areas is None else set(areas)
    rows = []
    if "testsets" in selected:
        rows.extend((test, False, ("input_sha", "answer_sha")) for config in document.get("testsets", {}).values()
                    for test in config.get("tests", {}).values())
    for area in ("validator_tests", "checker_tests"):
        if area in selected:
            rows.extend((test, bool(test.get("preserve_line_endings")), ("input_sha", "answer_sha", "output_sha"))
                        for test in document.get(area, {}).values())
    hashes = {test[key] for test, _, keys in rows for key in keys if test.get(key)}
    if hashes:
        registered = set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem_id)))
        if hashes - registered:
            fail("invalid_blob", "이 문제에 업로드하지 않은 파일을 사용할 수 없습니다.")
    replacements = {}
    report = {"files": 0, "tests": 0}
    for test, preserve, keys in rows:
        if preserve:
            continue
        changed = False
        for key in keys:
            sha = test.get(key)
            if not sha:
                continue
            if sha not in replacements:
                item = profile(sha)
                replacements[sha] = sha
                if item["needs_lf_conversion"] and not item["binary"]:
                    with blob_path(sha).open("rb") as source, tempfile.TemporaryFile("w+b") as destination:
                        _write_lf(source, destination, sha)
                        replacements[sha] = put_blob_file(db, problem_id, destination)["sha256"]
                    report["files"] += 1
            test[key] = replacements[sha]
            changed |= test[key] != sha
        for key in ("statement_input", "statement_output"):
            value = test.get(key)
            if value is None:
                continue
            try:
                normalized = normalize_test_bytes(value.encode("utf-8")).decode("utf-8")
            except UnicodeError:
                continue
            changed |= value != normalized
            test[key] = normalized
        report["tests"] += int(changed)
    return document, report
