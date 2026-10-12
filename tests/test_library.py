"""Exercise the server-only library boundary with real persisted projects."""
import base64
import hashlib
import hmac
import json
import time
from uuid import uuid4

import pytest
from pydantic import SecretStr
from app.config import get_settings
from app.db import Job, Problem, SessionLocal
from test_zoj import saved_package

KEY = "editor-library-test-secret-at-least-32-characters"
ROOT = "/api/zoj-library/problems"


@pytest.fixture(autouse=True)
def bridge(monkeypatch):
    monkeypatch.setattr(get_settings(), "zoj_bridge_secret", SecretStr(KEY))


def headers(method, path, email="author@example.com", body=b"", purpose="zoj-library-v1", stamp=None):
    encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
    stamp, nonce = str(stamp or int(time.time())), uuid4().hex
    sha = hashlib.sha256(body).hexdigest()
    message = "\n".join([purpose, method, path, stamp, nonce, encoded, sha])
    return {"X-Editor-Request": "1", "X-Editor-Identity": encoded,
            "X-Editor-Timestamp": stamp, "X-Editor-Nonce": nonce,
            "X-Editor-Content-Sha256": sha,
            "X-Editor-Signature": hmac.new(KEY.encode(), message.encode(), hashlib.sha256).hexdigest()}


def signed(client, method, path, email="author@example.com", values=None):
    body = json.dumps(values).encode() if values is not None else b""
    return client.request(method, path, content=body, headers=headers(method, path, email, body))


def test_signed_identity_purpose_expiry_replay_and_body(client, owner, problem):
    h = headers("GET", ROOT)
    assert client.get(ROOT, headers=h).status_code == 200
    assert client.get(ROOT, headers=h).status_code == 401
    assert client.get(ROOT, headers=headers("GET", ROOT, stamp=int(time.time())-120)).status_code == 401
    assert client.get(ROOT, headers=headers("GET", ROOT, purpose="zoj-editor-v1")).status_code == 401
    assert client.get(ROOT, headers={**headers("GET", ROOT), "X-Editor-Identity": "YXR0YWNrZXJAZXhhbXBsZS5jb20"}).status_code == 401
    path = ROOT + "/" + problem + "/packages"
    assert client.post(path, content=b"{}", headers=headers("POST", path, body=b"different")).status_code == 401
    assert client.get(ROOT).status_code == 401


def test_library_lists_only_own_projects_not_shared_access(client, owner, problem, clients):
    stranger = clients("stranger")
    other = stranger.post("/api/problems", json={"name": "private", "slug": "private"}).json()["id"]
    stranger.put(f"/api/problems/{other}/accesses", json={"login": "author", "role": "WRITE"})
    rows = signed(client, "GET", ROOT).json()
    assert [r["id"] for r in rows["problems"]] == [problem]
    assert rows["email"] == "author@example.com" and rows["account_exists"]
    assert signed(client, "POST", ROOT + "/" + other + "/packages", values={}).status_code == 404
    unknown = signed(client, "GET", ROOT, "missing@example.com").json()
    assert unknown["account_exists"] is False and unknown["problems"] == []
    owner.patch(f"/api/problems/{problem}/deleted", json={"deleted": True})
    assert signed(client, "GET", ROOT).json()["total"] == 0


def test_folders_filter_before_pagination_and_are_private(client, owner, problem, clients):
    folder = owner.post("/api/folders", json={"name": "대회"}).json()["id"]
    second = owner.post("/api/problems", json={"name": "100% match", "slug": "percent"}).json()["id"]
    owner.post("/api/folders/move-problems", json={"folder_id": folder, "problem_ids": [problem]})
    path = ROOT + "?folder=" + folder + "&limit=1"
    result = signed(client, "GET", path).json()
    assert result["total"] == 1 and result["problems"][0]["id"] == problem
    assert signed(client, "GET", ROOT + "?folder=root").json()["problems"][0]["id"] == second
    assert signed(client, "GET", ROOT + "?q=%25").json()["total"] == 1
    stranger = clients("stranger")
    foreign = stranger.post("/api/folders", json={"name": "개인"}).json()["id"]
    assert signed(client, "GET", ROOT + "?folder=" + foreign).status_code == 404


def test_committed_package_keeps_private_work_and_normal_api_requires_commit(client, owner, problem):
    job_id, _ = saved_package(problem, owner)
    state = owner.get("/api/problems/" + problem).json()
    state["document"]["info"]["name"] = "아직 공유하지 않은 제목"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]}).json()
    assert owner.post(f"/api/problems/{problem}/jobs?from_committed_revision=true", json={"kind": "package", "version": saved["version"], "options": {"export_format": "zoj"}}).status_code == 409
    path = ROOT + "/" + problem + "/packages"
    prepared = signed(client, "POST", path, values={})
    assert prepared.status_code == 202, prepared.text
    row = signed(client, "GET", ROOT).json()["problems"][0]
    assert row["uncommitted"] and row["name"] == "두 수의 합"
    assert owner.get("/api/problems/" + problem).json()["document"]["info"]["name"] == "아직 공유하지 않은 제목"
    with SessionLocal() as db:
        job = db.get(Job, prepared.json()["id"])
        assert job.document["info"]["name"] == "두 수의 합"
        assert job.options["verify"] and job.options["export_format"] == "zoj"
    assert signed(client, "POST", path, values={}).json()["id"] == prepared.json()["id"]
    for values in [{"verify": False}, {"email": "stranger@example.com"}]:
        assert signed(client, "POST", path, values=values).status_code == 422


def test_download_checks_owner_current_revision_verification_and_hash(client, owner, problem, clients):
    job_id, file = saved_package(problem, owner)
    path = f"{ROOT}/{problem}/packages/{job_id}"
    data = signed(client, "GET", path + "/download")
    assert data.status_code == 200 and data.content == file.read_bytes()
    clients("stranger")
    assert signed(client, "GET", path, "stranger@example.com").status_code == 404
    assert signed(client, "GET", path + "/download", "stranger@example.com").status_code == 404
    file.write_bytes(b"corrupted")
    assert signed(client, "GET", path + "/download").status_code == 409
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        job.status = "failed"
        db.commit()
    assert signed(client, "GET", path + "/download").status_code == 409
    with SessionLocal() as db:
        db.get(Problem, problem).revision += 1
        db.commit()
    assert signed(client, "GET", path).json()["error"]["code"] == "library_package_stale"
