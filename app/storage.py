import hashlib
import os
import tempfile
from pathlib import Path
from sqlalchemy import select, func, update
from .config import get_settings
from .db import Blob, Problem
from .domain import fail


def blob_path(sha):
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        fail("invalid_hash", "잘못된 파일 식별자입니다.")
    return get_settings().data_dir / "blobs" / sha[:2] / sha


def put_blob(db, problem_id, content: bytes):
    if len(content) > get_settings().max_upload_bytes:
        fail("too_large", "파일은 16MiB 이하로 올려 주세요.", 413)
    sha = hashlib.sha256(content).hexdigest()
    if not db.get(Blob, (problem_id, sha)):
        # Parallel jobs may produce the same content. Serialize quota checks
        # and metadata insertion across processes, not only within one job.
        if db.get_bind().dialect.name == "sqlite":
            db.execute(update(Problem).where(Problem.id == problem_id).values(updated_at=Problem.updated_at))
        else:
            db.scalar(select(Problem.id).where(Problem.id == problem_id).with_for_update())
        if db.get(Blob, (problem_id, sha)):
            return {"sha256": sha, "size": len(content)}
        used = db.scalar(select(func.coalesce(func.sum(Blob.size), 0)).where(Blob.problem_id == problem_id))
        if used + len(content) > get_settings().max_project_bytes:
            fail("project_quota", "문제 자료의 저장 한도에 도달했습니다.", 413)
    target = blob_path(sha)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        fd, tmp = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    if not db.get(Blob, (problem_id, sha)):
        db.add(Blob(problem_id=problem_id, sha256=sha, size=len(content)))
        db.flush()
    return {"sha256": sha, "size": len(content)}


def read_blob(sha):
    return blob_path(sha).read_bytes()
