"""File/body I/O must not borrow a connection while waiting for worker tokens."""
import asyncio
import io
import json
import zipfile

import httpx
from sqlalchemy import select

from app import downloads, problems
from app.db import Blob, Folder, Problem, SessionLocal, engine
from app.main import app
from app.schema import empty_document


def test_blob_download_releases_metadata_connection_before_file_response(owner, problem, monkeypatch):
    content = b"download bytes\r\n"
    sha = owner.post(f"/api/problems/{problem}/blobs", content=content).json()["sha256"]
    original = problems.FileResponse
    calls = []

    def file_response(*args, **kwargs):
        calls.append(engine.pool.checkedout())
        return original(*args, **kwargs)

    monkeypatch.setattr(problems, "FileResponse", file_response)
    response = owner.get(f"/api/problems/{problem}/blobs/{sha}")
    assert response.status_code == 200
    assert response.content == content
    assert calls == [0]


def test_zip_download_releases_connection_before_compression_and_streaming(owner, problem, monkeypatch):
    sha = owner.post(f"/api/problems/{problem}/blobs", content=b"zip bytes").json()["sha256"]
    original = downloads.tempfile.TemporaryFile
    calls = []

    def archive_file(*args, **kwargs):
        calls.append(engine.pool.checkedout())
        return original(*args, **kwargs)

    monkeypatch.setattr(downloads.tempfile, "TemporaryFile", archive_file)
    response = owner.post(f"/api/problems/{problem}/downloads", json={"files": [{"path": "data.in", "sha256": sha}]})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read("data.in") == b"zip bytes"
    assert calls == [0]


def request_with_body(client, url, body):
    async def execute():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=client.cookies,
                                     headers={"X-Editor-Request": "1"}) as remote:
            return await remote.post(url, content=body())
    return asyncio.run(execute())


def test_blob_upload_does_not_hold_connection_between_body_chunks(owner, problem):
    observed = []

    async def body():
        observed.append(engine.pool.checkedout())
        yield b"first chunk"
        await asyncio.sleep(0)
        observed.append(engine.pool.checkedout())
        yield b"second chunk"

    response = request_with_body(owner, f"/api/problems/{problem}/blobs", body)
    assert response.status_code == 201, response.text
    assert observed == [0, 0]
    assert owner.get(f'/api/problems/{problem}/blobs/{response.json()["sha256"]}').content == b"first chunksecond chunk"


def test_blob_upload_rechecks_editing_permission_after_body(owner, problem, clients):
    writer = clients("writer")
    grant = owner.put(f"/api/problems/{problem}/accesses", json={"login": "writer", "role": "WRITE"})
    assert grant.status_code == 200

    async def body():
        assert engine.pool.checkedout() == 0
        yield b"not admitted"
        revoke = owner.put(f"/api/problems/{problem}/accesses", json={"login": "writer", "role": "READ"})
        assert revoke.status_code == 200

    response = request_with_body(writer, f"/api/problems/{problem}/blobs", body)
    assert response.status_code == 403
    with SessionLocal() as db:
        assert db.scalars(select(Blob).where(Blob.problem_id == problem)).all() == []


def import_archive():
    content = io.BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("editor.json", json.dumps({"format": "zoj-editor-v1", "document": empty_document("Imported")}))
    return content.getvalue()


def test_archive_upload_releases_folder_connection_while_receiving_body(owner):
    folder = owner.post("/api/folders", json={"name": "Imports"}).json()["id"]
    content = import_archive()
    observed = []

    async def body():
        observed.append(engine.pool.checkedout())
        yield content[:20]
        await asyncio.sleep(0)
        observed.append(engine.pool.checkedout())
        yield content[20:]

    response = request_with_body(owner, f"/api/imports?name=Imported&slug=imported-task&folder_id={folder}", body)
    assert response.status_code == 201, response.text
    assert observed == [0, 0]


def test_archive_upload_rechecks_destination_after_body(owner):
    folder = owner.post("/api/folders", json={"name": "Removed destination"}).json()["id"]

    async def body():
        assert engine.pool.checkedout() == 0
        yield import_archive()
        with SessionLocal() as db:
            db.delete(db.get(Folder, folder))
            db.commit()

    response = request_with_body(owner, f"/api/imports?name=Imported&slug=revoked-destination&folder_id={folder}", body)
    assert response.status_code == 404
    with SessionLocal() as db:
        assert db.scalar(select(Problem).where(Problem.slug == "revoked-destination")) is None
