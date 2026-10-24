import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="zoj-editor-tests-"))
os.environ["EDITOR_ENV"] = "test"
os.environ["EDITOR_EXECUTION_TRANSPORT"] = "local"
os.environ["EDITOR_AUTH_MODE"] = "local"
os.environ["EDITOR_DATABASE_URL"] = os.environ.get("EDITOR_TEST_DATABASE_URL", "sqlite:///" + str(TEST_ROOT / "test.db"))
os.environ["EDITOR_DATA_DIR"] = str(TEST_ROOT)
os.environ["EDITOR_WORK_DIR"] = str(TEST_ROOT / "work")
os.environ["EDITOR_MAIL_MODE"] = "console"

import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import Base, engine, LoginSession, User, SessionLocal, now
from app.auth import digest


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app, headers={"X-Editor-Request": "1"}) as client:
        yield client


@pytest.fixture
def clients():
    result = []
    def make(handle="author"):
        with SessionLocal() as db:
            user = User(email=handle + "@example.com", handle=handle, display_name=handle)
            db.add(user)
            db.flush()
            token = "test-token-" + handle
            db.add(LoginSession(digest=digest(token), user_id=user.id, expires_at=now() + 3600))
            db.commit()
        client = TestClient(app, headers={"X-Editor-Request": "1"})
        client.cookies.set("editor_session", token)
        result.append(client)
        return client
    yield make
    for client in result:
        client.close()


@pytest.fixture
def owner(clients):
    return clients()


@pytest.fixture
def problem(owner):
    response = owner.post("/api/problems", json={"name": "두 수의 합", "slug": "a-plus-b"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture(autouse=True)
def judge_capabilities_transport(monkeypatch):
    """Unit tests negotiate a judge protocol without contacting production."""
    from app import judge_bridge
    real_remote = judge_bridge.remote
    def remote(method, path, *args, **kwargs):
        if method == "GET" and path == "/api/editor/judge/capabilities":
            return {"backend":"zoj", "runtime_revision":"test-judge-v1", "online_nodes":1,
                    "reference_output_protocol":"editor-reference-output-v1", "reference_output_online_nodes":1,
                    "execution":{"protocol":"editor-execute-v1","runtime_revision":"test-execution-v1","online_nodes":1}}
        return real_remote(method, path, *args, **kwargs)
    monkeypatch.setattr(judge_bridge, "remote", remote)


@pytest.fixture
def queue_legacy_job(monkeypatch):
    """Explicit legacy test double; online workers never execute locally."""
    import copy
    from httpx import Response
    from app.config import get_settings
    from app.db import Job, Problem
    from app.jobs import JobRequest, job_json
    from app import pipeline
    # Historical engine tests inject an explicit test double; service workers
    # never have a local execution path or an admission bypass.
    monkeypatch.setattr(pipeline, 'require_agent_job', lambda job: None)
    if get_settings().execution_transport == 'local':
        from app.sandbox import Sandbox
        monkeypatch.setattr(pipeline, 'Sandbox', Sandbox)
    def enqueue(client, problem_id, payload):
        state = client.get(f"/api/problems/{problem_id}").json()
        request = JobRequest.model_validate(payload)
        assert request.version == state['version']
        options = request.options.model_dump()
        options.update(execution_backend='editor', snapshot='working_copy',
                       runtime_revision=get_settings().runtime_revision,
                       execution_transport=get_settings().execution_transport)
        options['candidates'] = options['candidates'] or ([options['candidate']] if options['candidate'] else [])
        with SessionLocal() as db:
            problem = db.get(Problem, problem_id)
            job = Job(problem_id=problem_id, user_id=problem.owner_id, kind=request.kind,
                      document=copy.deepcopy(state['document']), document_hash=state['document_hash'],
                      workspace_version=state['version'], revision=problem.revision, options=options)
            db.add(job)
            db.commit()
            return Response(202, json=job_json(job))
    return enqueue
