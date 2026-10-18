import hashlib
import io
import shutil
import os
import tempfile
from pathlib import Path
from sqlalchemy import select, func, update
from .config import get_settings
from .db import Blob, Problem
from .domain import fail
from .file_limits import file_limit_message


def blob_path(sha):
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        fail("invalid_hash", "잘못된 파일 식별자입니다.")
    return get_settings().data_dir / "blobs" / sha[:2] / sha


def put_blob(db, problem_id, content: bytes):
    return put_blob_file(db, problem_id, io.BytesIO(content))


def put_blob_file(db, problem_id, source):
    source.seek(0, os.SEEK_END)
    size = source.tell()
    limit = get_settings().max_upload_bytes
    if size > limit:
        fail("too_large", file_limit_message(limit), 413)
    source.seek(0)
    sha = hashlib.file_digest(source, "sha256").hexdigest()
    if not db.get(Blob, (problem_id, sha)):
        # Serialize quota checks and metadata insertion across worker processes.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(update(Problem).where(Problem.id == problem_id).values(updated_at=Problem.updated_at))
        else:
            db.scalar(select(Problem.id).where(Problem.id == problem_id).with_for_update())
        if db.get(Blob, (problem_id, sha)):
            return {"sha256": sha, "size": size}
        used = db.scalar(select(func.coalesce(func.sum(Blob.size), 0)).where(Blob.problem_id == problem_id))
        if used + size > get_settings().max_project_bytes:
            fail("project_quota", "문제 자료의 저장 한도에 도달했습니다.", 413)
    target = blob_path(sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    valid = False
    if target.exists():
        with target.open("rb") as stored:
            valid = hashlib.file_digest(stored, "sha256").hexdigest() == sha
    if not valid:
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        try:
            source.seek(0)
            with os.fdopen(fd, "wb") as stream:
                shutil.copyfileobj(source, stream, 1024**2)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    if not db.get(Blob, (problem_id, sha)):
        db.add(Blob(problem_id=problem_id, sha256=sha, size=size))
        db.flush()
    return {"sha256": sha, "size": size}


def read_blob(sha):
    return blob_path(sha).read_bytes()
