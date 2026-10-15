"""Download selected problem blobs without changing the working copy."""
import tempfile
import zipfile

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .auth import current_user
from .config import get_settings
from .db import Blob, User, session_dependency
from .domain import authorize, fail
from .schema import Strict, safe_name
from .storage import blob_path

router = APIRouter(prefix="/api/problems", tags=["downloads"])


class DownloadFile(Strict):
    path: str = Field(min_length=1, max_length=320)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def archive_path(cls, value):
        parts = value.split("/")
        if len(parts) > 4:
            raise ValueError("Too many archive directories")
        for part in parts:
            safe_name(part)
        return value


class DownloadRequest(Strict):
    filename: str = Field(default="files.zip", min_length=1, max_length=100)
    files: list[DownloadFile] = Field(min_length=1, max_length=4000)

    @field_validator("filename")
    @classmethod
    def archive_name(cls, value):
        safe_name(value)
        if not value.endswith(".zip"):
            raise ValueError("Use a ZIP archive filename")
        return value


@router.post("/{problem_id}/downloads")
def download_files(problem_id: str, values: DownloadRequest,
                   user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    paths = [entry.path.casefold() for entry in values.files]
    path_set = set(paths)
    if len(paths) != len(path_set) or any(
        "/".join(path.split("/")[:index]) in path_set
        for path in paths for index in range(1, len(path.split("/")))
    ):
        fail("duplicate_download_path", "다운로드 파일 이름이 겹칩니다.")
    hashes = list({entry.sha256 for entry in values.files})
    sizes = {}
    for start in range(0, len(hashes), 500):
        sizes.update(db.execute(select(Blob.sha256, Blob.size).where(
            Blob.problem_id == problem_id, Blob.sha256.in_(hashes[start:start + 500])
        )).all())
    if set(hashes) != set(sizes) or any(not blob_path(sha).is_file() for sha in hashes):
        fail("not_found", "이 문제의 다운로드 파일을 찾을 수 없습니다.", 404)
    # Count every ZIP entry, including repeated content, to bound amplification.
    if sum(sizes[entry.sha256] for entry in values.files) > get_settings().max_project_bytes:
        fail("download_too_large", "다운로드 크기가 문제 자료의 저장 한도를 넘습니다. 나누어 선택하세요.", 413)
    archive = tempfile.TemporaryFile()
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as output:
            for entry in values.files:
                output.write(blob_path(entry.sha256), entry.path)
        length = archive.tell()
        archive.seek(0)
    except BaseException:
        archive.close()
        raise

    def chunks():
        try:
            while chunk := archive.read(1024 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{values.filename}"',
        "Content-Length": str(length),
    }, background=BackgroundTask(archive.close))
