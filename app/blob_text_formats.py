"""Inspect stored text and create LF copies without changing problem documents."""
import codecs
import hashlib
import os
import tempfile
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import current_user
from .db import Blob, User, session_dependency
from .domain import authorize, fail
from .permissions import editing
from .schema import Strict
from .storage import blob_path, put_blob_file

router = APIRouter(prefix="/api/problems", tags=["text formats"])
CHUNK_BYTES = 1024 * 1024


class TextFormatRequest(Strict):
    sha256: list[Annotated[str, Field(strict=True, pattern=r"^[a-f0-9]{64}$")]] = Field(min_length=1, max_length=100)


def _scan(stream):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    cr = lf = crlf = size = 0
    previous_cr = False
    last = None
    binary = False
    while chunk := stream.read(CHUNK_BYTES):
        size += len(chunk)
        cr += chunk.count(b"\r")
        lf += chunk.count(b"\n")
        crlf += chunk.count(b"\r\n") + int(previous_cr and chunk.startswith(b"\n"))
        previous_cr = chunk.endswith(b"\r")
        last = chunk[-1]
        if b"\0" in chunk:
            binary = True
        if not binary:
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                binary = True
    if not binary:
        try:
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            binary = True
    if binary:
        return size, None, None, False, True
    endings = [name for name, count in (("lf", lf - crlf), ("crlf", crlf), ("cr", cr - crlf)) if count]
    ending = "mixed" if len(endings) > 1 else endings[0] if endings else "none"
    return size, ending, last in (10, 13), cr > 0, False


def _stat_key(stat):
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


@lru_cache(maxsize=4096)
def _cached_profile(path, key):
    with open(path, "rb") as source:
        if _stat_key(os.fstat(source.fileno())) != key:
            fail("blob_changed", "파일이 변경되었습니다. 목록을 다시 불러오세요.", 409)
        result = _scan(source)
        if _stat_key(os.fstat(source.fileno())) != key or result[0] != key[0]:
            fail("blob_changed", "파일이 변경되었습니다. 목록을 다시 불러오세요.", 409)
    return result


def profile(sha):
    path = blob_path(sha)
    try:
        stat = path.stat()
        size, ending, final, needs, binary = _cached_profile(str(path), _stat_key(stat))
    except OSError:
        fail("not_found", "파일을 찾을 수 없습니다.", 404)
    return {"sha256": sha, "size": size, "line_ending": ending,
            "has_final_newline": final, "needs_lf_conversion": needs, "binary": binary}


def registered_hashes(db, problem_id, values):
    hashes = list(dict.fromkeys(values.sha256))
    registered = set(db.scalars(select(Blob.sha256).where(Blob.problem_id == problem_id, Blob.sha256.in_(hashes))))
    if registered != set(hashes):
        fail("not_found", "이 문제에 등록된 파일을 찾을 수 없습니다.", 404)
    return hashes


def _write_lf(source, destination, expected_sha):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    digest = hashlib.sha256()
    carry = b""
    try:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
            if b"\0" in chunk:
                raise UnicodeError("Binary input")
            decoder.decode(chunk)
            chunk = carry + chunk
            carry = b"\r" if chunk.endswith(b"\r") else b""
            if carry:
                chunk = chunk[:-1]
            destination.write(chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))
        decoder.decode(b"", final=True)
    except UnicodeError:
        fail("invalid_text", "바이너리 또는 UTF-8이 아닌 파일은 줄바꿈을 변환할 수 없습니다.")
    if digest.hexdigest() != expected_sha:
        fail("blob_changed", "저장된 파일의 해시가 일치하지 않습니다. 목록을 다시 불러오세요.", 409)
    if carry:
        destination.write(b"\n")


@router.post("/{problem_id}/blobs/text-formats")
def text_formats(problem_id: str, values: TextFormatRequest, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    hashes = registered_hashes(db, problem_id, values)
    profiles = {sha: profile(sha) for sha in hashes}
    return {"items": [profiles[sha] for sha in values.sha256]}


@router.post("/{problem_id}/blobs/normalize-lf")
def normalize_lf(problem_id: str, values: TextFormatRequest, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    _, access = authorize(db, problem_id, user)
    if not editing(access):
        fail("forbidden", "업로드 권한이 필요합니다.", 403)
    hashes = registered_hashes(db, problem_id, values)
    profiles = {sha: profile(sha) for sha in hashes}
    if any(item["binary"] for item in profiles.values()):
        fail("invalid_text", "바이너리 또는 UTF-8이 아닌 파일은 줄바꿈을 변환할 수 없습니다.")
    normalized = {}
    try:
        for sha in hashes:
            if not profiles[sha]["needs_lf_conversion"]:
                normalized[sha] = sha
                continue
            with blob_path(sha).open("rb") as source, tempfile.TemporaryFile("w+b") as destination:
                _write_lf(source, destination, sha)
                normalized[sha] = put_blob_file(db, problem_id, destination)["sha256"]
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {"items": [{**profiles[sha], "normalized_sha256": normalized[sha]} for sha in values.sha256]}
