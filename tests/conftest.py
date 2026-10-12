import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="zoj-editor-tests-"))
os.environ["EDITOR_ENV"] = "test"
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
