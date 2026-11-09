"""Authentication releases its own pool slot before the endpoint starts work."""
import asyncio

import anyio
import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request

from app import auth, db as database, revision_updates, shared_auth
from app.config import get_settings
from app.db import Base, LoginSession, User, now


@pytest.fixture
def identity_pool(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "identity.db"),
        connect_args={"check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    Base.metadata.create_all(engine)
    closed = []

    class TrackedSession(Session):
        def close(self):
            super().close()
            closed.append(self)

    factory = sessionmaker(engine, class_=TrackedSession, expire_on_commit=False)
    monkeypatch.setattr(auth, "SessionLocal", factory)
    # This also makes a regression to request-scope Depends use the small pool.
    monkeypatch.setattr(database, "SessionLocal", factory)
    identity = {
        "account_id": "central-fixture",
        "email": "identity@example.com",
        "display_name": "Identity fixture",
    }
    monkeypatch.setattr(shared_auth, "call", lambda *args, **kwargs: dict(identity))
    with factory() as db:
        db.add(User(
            id="fixture-user", email=identity["email"],
            central_account_id=identity["account_id"], handle="identity",
            display_name=identity["display_name"],
        ))
        db.add(LoginSession(
            digest=auth.digest("fixture-token"), user_id="fixture-user",
            expires_at=now() + 300,
        ))
        db.commit()
    closed.clear()
    yield engine, factory, identity, closed
    assert engine.pool.checkedout() == 0
    engine.dispose()


def request_for(mode, token="fixture-token"):
    cookie = "zoj_sso" if mode == "zoj" else auth.COOKIE
    return Request({
        "type": "http", "method": "GET", "path": "/identity",
        "headers": [(b"cookie", f"{cookie}={token}".encode())],
        "client": ("127.0.0.1", 0),
    })


@pytest.mark.parametrize("mode", ["local", "zoj"])
def test_success_returns_detached_scalar_identity_and_no_connection(identity_pool, monkeypatch, mode):
    engine, _, identity, closed = identity_pool
    identity["is_service_master"] = True
    monkeypatch.setattr(get_settings(), "auth_mode", mode)
    user = auth.current_user(request_for(mode))
    assert inspect(user).detached
    assert auth.user_json(user) == {
        "id": "fixture-user", "account_id": identity["account_id"],
        "email": identity["email"], "handle": "identity",
        "display_name": identity["display_name"],
        "is_service_master": mode == "zoj",
    }
    assert len(closed) == 1
    assert engine.pool.checkedout() == 0
    assert revision_updates.viewer_id(request_for(mode)) == "fixture-user"
    assert len(closed) == 2
    assert engine.pool.checkedout() == 0


@pytest.mark.parametrize("reason", ["missing", "unknown", "expired"])
def test_local_auth_failures_close_the_session(identity_pool, monkeypatch, reason):
    engine, factory, _, closed = identity_pool
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    if reason == "expired":
        with factory() as db:
            db.get(LoginSession, auth.digest("fixture-token")).expires_at = now() - 1
            db.commit()
        closed.clear()
    token = "" if reason == "missing" else "unknown" if reason == "unknown" else "fixture-token"
    with pytest.raises(HTTPException) as error:
        auth.current_user(request_for("local", token))
    assert error.value.status_code == 401
    assert len(closed) == 1
    assert engine.pool.checkedout() == 0


def test_shared_auth_failure_closes_session_and_preserves_error(identity_pool, monkeypatch):
    engine, _, _, closed = identity_pool
    monkeypatch.setattr(get_settings(), "auth_mode", "zoj")

    def unavailable(*args, **kwargs):
        raise HTTPException(503, {"code": "shared_auth_unavailable"})

    monkeypatch.setattr(shared_auth, "call", unavailable)
    with pytest.raises(HTTPException) as error:
        auth.current_user(request_for("zoj"))
    assert error.value.status_code == 503
    assert error.value.detail["code"] == "shared_auth_unavailable"
    assert len(closed) == 1
    assert engine.pool.checkedout() == 0


def test_shared_profile_sync_commits_then_returns_a_detached_user(identity_pool, monkeypatch):
    engine, factory, identity, closed = identity_pool
    monkeypatch.setattr(get_settings(), "auth_mode", "zoj")
    identity["display_name"] = "Updated central name"
    user = auth.current_user(request_for("zoj"))
    assert inspect(user).detached
    assert user.display_name == identity["display_name"]
    assert len(closed) == 1
    assert engine.pool.checkedout() == 0
    with factory() as db:
        assert db.get(User, user.id).display_name == identity["display_name"]


@pytest.mark.parametrize("mode", ["local", "zoj"])
def test_async_endpoint_handoff_does_not_starve_identity_pool(identity_pool, monkeypatch, mode):
    engine, _, _, _ = identity_pool
    monkeypatch.setattr(get_settings(), "auth_mode", mode)

    async def exercise():
        app = FastAPI(dependencies=[Depends(auth.check_browser_request)])
        entered = asyncio.Event()
        release = asyncio.Event()
        checked_out_at_handoff = []
        detached_at_handoff = []

        @app.get("/probe")
        async def probe(user: User = Depends(auth.current_user)):
            checked_out_at_handoff.append(engine.pool.checkedout())
            detached_at_handoff.append(inspect(user).detached)
            entered.set()
            await release.wait()
            # FileResponse and streamed upload I/O use this same worker pool.
            return await run_in_threadpool(auth.user_json, user)

        limiter = anyio.to_thread.current_default_thread_limiter()
        original_limit = limiter.total_tokens
        limiter.total_tokens = 2
        tasks = []
        cookie = "zoj_sso" if mode == "zoj" else auth.COOKIE
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://isolated.test", cookies={cookie: "fixture-token"},
            ) as client:
                tasks.append(asyncio.create_task(client.get("/probe")))
                await asyncio.wait_for(entered.wait(), 1)
                tasks.extend(asyncio.create_task(client.get("/probe")) for _ in range(2))
                await asyncio.sleep(0.03)
                release.set()
                responses = await asyncio.wait_for(asyncio.gather(*tasks), 2)
            assert [response.status_code for response in responses] == [200, 200, 200]
            # Other concurrent identities may briefly check out the sole slot;
            # the first handoff happens before they start and must own none.
            assert checked_out_at_handoff[0] == 0
            assert detached_at_handoff == [True, True, True]
            assert all(response.json()["id"] == "fixture-user" for response in responses)
            assert engine.pool.checkedout() == 0
            assert limiter.statistics().borrowed_tokens == 0
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            limiter.total_tokens = original_limit

    asyncio.run(exercise())
