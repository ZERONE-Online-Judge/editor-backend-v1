import hashlib
import io
import os

import pytest
from sqlalchemy import func, select

from app import blob_text_formats as formats
from app.config import get_settings
from app.db import Access, Blob, Job, Problem, Revision, SessionLocal, User, WorkingCopy
from app.storage import blob_path, read_blob


def upload(owner, problem, content):
    response = owner.post(f"/api/problems/{problem}/blobs", content=content)
    assert response.status_code == 201, response.text
    return response.json()["sha256"]


def request(client, problem, hashes, action="text-formats"):
    return client.post(f"/api/problems/{problem}/blobs/{action}", json={"sha256": hashes})


def registered(problem):
    with SessionLocal() as db:
        return set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem)))


@pytest.fixture(autouse=True)
def clear_profiles():
    formats._cached_profile.cache_clear()
    yield
    formats._cached_profile.cache_clear()


@pytest.mark.parametrize("content,ending,final,needs,binary", [
    (b"", "none", False, False, False),
    (b"7 8", "none", False, False, False),
    (b"7\n8", "lf", False, False, False),
    (b"7\n8\n", "lf", True, False, False),
    (b"7\r\n8", "crlf", False, True, False),
    (b"7\r\n8\r\n", "crlf", True, True, False),
    (b"7\r8\r", "cr", True, True, False),
    (b"7\r\n8\r9\n", "mixed", True, True, False),
    ("\ufeff텍스트 🎲\r\n다음".encode(), "crlf", False, True, False),
    ("문자\u2028열\u2029".encode(), "none", False, False, False),
    (b"7\0\r\n", None, None, False, True),
    (b"7\xff\r\n", None, None, False, True),
    (b"7\xe2\x82", None, None, False, True),
])
def test_profiles_report_exact_stored_format(owner, problem, content, ending, final, needs, binary):
    sha = upload(owner, problem, content)
    response = request(owner, problem, [sha])
    assert response.status_code == 200, response.text
    assert response.json() == {"items": [{"sha256": sha, "size": len(content), "line_ending": ending,
        "has_final_newline": final, "needs_lf_conversion": needs, "binary": binary}]}
    assert read_blob(sha) == content


def test_readers_can_inspect_but_not_convert_and_hidden_problems_stay_hidden(owner, problem, clients, client):
    sha = upload(owner, problem, b"7\r\n8\r\n")
    reader = clients("reader")
    stranger = clients("stranger")
    with SessionLocal() as db:
        account = db.scalar(select(User).where(User.handle == "reader"))
        db.add(Access(problem_id=problem, user_id=account.id, role="WRITE", permissions=[]))
        db.commit()
    assert request(reader, problem, [sha]).status_code == 200
    assert request(reader, problem, [sha], "normalize-lf").status_code == 403
    for action in ("text-formats", "normalize-lf"):
        assert request(stranger, problem, [sha], action).status_code == 404
        assert request(client, problem, [sha], action).status_code == 401
        denied = owner.post(f"/api/problems/{problem}/blobs/{action}", json={"sha256": [sha]}, headers={"X-Editor-Request": "0"})
        assert denied.status_code == 403
    assert registered(problem) == {sha}


def test_every_hash_is_authorized_before_inspection_or_conversion(owner, problem, monkeypatch):
    local = upload(owner, problem, b"7\r\n")
    other = owner.post("/api/problems", json={"name": "Other", "slug": "other-problem"}).json()["id"]
    foreign = upload(owner, other, b"private\r\n")
    def unexpected(*args):
        raise AssertionError("An unregistered selection must fail before any file is inspected")
    monkeypatch.setattr(formats, "profile", unexpected)
    for action in ("text-formats", "normalize-lf"):
        assert request(owner, problem, [local, foreign], action).status_code == 404
        assert request(owner, problem, ["f" * 64], action).status_code == 404
    assert registered(problem) == {local}


@pytest.mark.parametrize("values", [[], ["invalid"], ["A" * 64], [123], ["a" * 64] * 101])
def test_requests_bound_and_validate_hashes(owner, problem, values):
    for action in ("text-formats", "normalize-lf"):
        assert request(owner, problem, values, action).status_code == 422


def test_conversion_preserves_originals_spacing_final_newline_and_reuses_hashes(owner, problem):
    originals = [b"3  \r\n7 8\t", b"-1\r2  3 \r", b"", b"already\n"]
    hashes = [upload(owner, problem, value) for value in originals]
    expected = [value.replace(b"\r\n", b"\n").replace(b"\r", b"\n") for value in originals]
    # A normalized file already registered on the same problem must be reused.
    existing = upload(owner, problem, expected[0])
    before = registered(problem)
    response = request(owner, problem, [*hashes, hashes[0]], "normalize-lf")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["sha256"] for item in items] == [*hashes, hashes[0]]
    assert items[0]["normalized_sha256"] == existing == items[-1]["normalized_sha256"]
    for source, value, normalized, item in zip(hashes, originals, expected, items):
        assert read_blob(source) == value
        assert read_blob(item["normalized_sha256"]) == normalized
        assert item["normalized_sha256"] == hashlib.sha256(normalized).hexdigest()
        assert item["size"] == len(value)  # Profiles describe the requested original.
    assert items[2]["normalized_sha256"] == hashes[2]
    assert items[3]["normalized_sha256"] == hashes[3]
    after = registered(problem)
    assert after - before == {hashlib.sha256(expected[1]).hexdigest()}
    repeated = request(owner, problem, hashes, "normalize-lf")
    assert repeated.status_code == 200
    assert registered(problem) == after


@pytest.mark.parametrize("invalid", [b"binary\0\r\n", b"invalid\xff\r\n", b"truncated\xe2\x82"])
def test_binary_or_invalid_utf8_rejects_entire_conversion_before_creating_blobs(owner, problem, invalid):
    good = upload(owner, problem, b"convert\r\n")
    bad = upload(owner, problem, invalid)
    before = registered(problem)
    response = request(owner, problem, [good, bad], "normalize-lf")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_text"
    assert registered(problem) == before
    assert read_blob(good) == b"convert\r\n"


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 64])
def test_utf8_and_crlf_chunk_boundaries_are_preserved(owner, problem, monkeypatch, chunk_size):
    monkeypatch.setattr(formats, "CHUNK_BYTES", chunk_size)
    content = b"123456\r\n" + "한글 🎲".encode() + b"\r\n\r7  \n8\r"
    sha = upload(owner, problem, content)
    inspected = request(owner, problem, [sha]).json()["items"][0]
    assert inspected["line_ending"] == "mixed" and not inspected["binary"]
    response = request(owner, problem, [sha], "normalize-lf")
    assert response.status_code == 200, response.text
    expected = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    assert read_blob(response.json()["items"][0]["normalized_sha256"]) == expected


def test_scanning_and_conversion_read_bounded_chunks(monkeypatch):
    monkeypatch.setattr(formats, "CHUNK_BYTES", 4096)
    content = (b"123456\r\n" * 400000) + b"last\r"
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 4096, "The complete file must not be loaded into memory"
            return super().read(size)
    result = formats._scan(Bounded(content))
    assert result == (len(content), "mixed", True, True, False)
    destination = io.BytesIO()
    formats._write_lf(Bounded(content), destination, hashlib.sha256(content).hexdigest())
    assert destination.getvalue() == content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def test_profiles_are_cached_by_immutable_file_identity_and_rechecked_after_repair(owner, problem, monkeypatch):
    sha = upload(owner, problem, b"cached\r\n")
    original = formats._scan
    scans = []
    def scan(stream):
        scans.append(True)
        return original(stream)
    monkeypatch.setattr(formats, "_scan", scan)
    assert request(owner, problem, [sha, sha]).status_code == 200
    assert request(owner, problem, [sha]).status_code == 200
    assert len(scans) == 1
    path = blob_path(sha)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
    assert request(owner, problem, [sha]).status_code == 200
    assert len(scans) == 2


def test_failed_quota_rolls_back_all_new_blob_registrations(owner, problem, monkeypatch):
    originals = [b"first\r\n", b"second\r\n"]
    hashes = [upload(owner, problem, value) for value in originals]
    before = registered(problem)
    monkeypatch.setattr(get_settings(), "max_project_bytes", sum(map(len, originals)) + len(b"first\n"))
    response = request(owner, problem, hashes, "normalize-lf")
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "project_quota"
    assert registered(problem) == before


def test_format_endpoints_never_change_workspaces_revisions_or_jobs(owner, problem):
    sha = upload(owner, problem, b"reference\r\n")
    owner.get(f"/api/problems/{problem}")  # Prepare a real existing workspace.
    def snapshot():
        with SessionLocal() as db:
            work = db.scalar(select(WorkingCopy).where(WorkingCopy.problem_id == problem))
            doc = db.get(Problem, problem)
            return (work.version, work.document, work.base_document, work.base_revision, work.updated_at,
                    doc.document, doc.revision, db.scalar(select(func.count()).select_from(Revision)),
                    db.scalar(select(func.count()).select_from(Job)))
    before = snapshot()
    assert request(owner, problem, [sha]).status_code == 200
    assert request(owner, problem, [sha], "normalize-lf").status_code == 200
    assert snapshot() == before


def test_missing_registered_file_returns_not_found(owner, problem):
    sha = upload(owner, problem, b"removed\r\n")
    blob_path(sha).unlink()
    assert request(owner, problem, [sha]).status_code == 404
    assert request(owner, problem, [sha], "normalize-lf").status_code == 404
