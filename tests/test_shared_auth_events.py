"""Session-event connections rotate without changing the browser's login."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from app import auth, shared_auth


class TrackedStream(httpx.AsyncByteStream):
    def __init__(self, source):
        self.source = source
        self.closed = False
        self.iterator_closed = False
        self.iterator = None

    def __aiter__(self):
        self.iterator = self.iterate()
        return self.iterator

    async def iterate(self):
        try:
            async for chunk in self.source():
                yield chunk
        finally:
            self.iterator_closed = True

    async def aclose(self):
        self.closed = True
        if self.iterator is not None:
            await self.iterator.aclose()


def upstream(monkeypatch, source, status=200):
    stream = TrackedStream(source)
    clients = []
    real_client = httpx.AsyncClient

    async def receive(_request):
        return httpx.Response(status, stream=stream)

    def client(**kwargs):
        result = real_client(transport=httpx.MockTransport(receive), **kwargs)
        clients.append(result)
        return result

    monkeypatch.setattr(shared_auth, "signed", lambda *_: ("https://central.test/events", b"{}", {}))
    monkeypatch.setattr(shared_auth.httpx, "AsyncClient", client)
    return stream, clients


async def connected():
    return False


async def collect(request):
    return [chunk async for chunk in shared_auth.session_events(request)]


@pytest.mark.parametrize("busy", [False, True])
def test_absolute_rotation_closes_quiet_and_heartbeat_streams(monkeypatch, busy):
    async def exercise():
        sent = 0

        async def source():
            nonlocal sent
            while True:
                sent += 1
                yield b": heartbeat\n\n"
                if busy:
                    await asyncio.sleep(0)
                else:
                    await asyncio.Event().wait()

        stream, clients = upstream(monkeypatch, source)
        monkeypatch.setattr(shared_auth, "SESSION_EVENTS_MAX_SECONDS", 0.03)
        chunks = await asyncio.wait_for(collect(SimpleNamespace(
            cookies={}, client=SimpleNamespace(host="127.0.0.1"), is_disconnected=connected,
        )), 1)
        assert sent >= (2 if busy else 1)
        assert chunks and all(chunk == b": heartbeat\n\n" for chunk in chunks)
        assert stream.closed and stream.iterator_closed
        assert len(clients) == 1 and clients[0].is_closed

    asyncio.run(exercise())


@pytest.mark.parametrize("status", [200, 401])
def test_real_revocation_is_preserved_and_transport_is_closed(monkeypatch, status):
    async def exercise():
        async def source():
            yield b"event: session_revoked\ndata: {}\n\n"

        stream, clients = upstream(monkeypatch, source, status)
        chunks = await collect(SimpleNamespace(
            cookies={}, client=SimpleNamespace(host="127.0.0.1"), is_disconnected=connected,
        ))
        assert len(chunks) == 1
        assert chunks[0] in (b"event: session_revoked\ndata: {}\n\n", "event: session_revoked\ndata: {}\n\n")
        assert stream.closed and clients[0].is_closed

    asyncio.run(exercise())


def test_task_cancellation_propagates_and_releases_remote_stream(monkeypatch):
    async def exercise():
        entered = asyncio.Event()

        async def source():
            entered.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        stream, clients = upstream(monkeypatch, source)
        task = asyncio.create_task(collect(SimpleNamespace(
            cookies={}, client=SimpleNamespace(host="127.0.0.1"), is_disconnected=connected,
        )))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert stream.closed and stream.iterator_closed and clients[0].is_closed

    asyncio.run(exercise())


def test_rotation_does_not_cancel_consumer_between_chunks(monkeypatch):
    async def exercise():
        async def source():
            yield b": heartbeat\n\n"
            yield b": next heartbeat\n\n"

        stream, clients = upstream(monkeypatch, source)
        monkeypatch.setattr(shared_auth, "SESSION_EVENTS_MAX_SECONDS", 0.02)
        events = shared_auth.session_events(SimpleNamespace(
            cookies={}, client=SimpleNamespace(host="127.0.0.1"), is_disconnected=connected,
        ))
        try:
            assert await anext(events) == b": heartbeat\n\n"
            # ASGI may be awaiting a slow browser send here. The generator's
            # deadline must not cancel work performed outside the generator.
            await asyncio.sleep(0.05)
            assert asyncio.current_task().cancelling() == 0
            with pytest.raises(StopAsyncIteration):
                await anext(events)
        finally:
            await events.aclose()
        assert stream.closed and clients[0].is_closed

    asyncio.run(exercise())


@pytest.mark.parametrize("disconnect", [False, True])
def test_streaming_response_rotates_or_disconnects_without_cookie_changes(monkeypatch, disconnect):
    async def exercise():
        entered, gone = asyncio.Event(), asyncio.Event()
        messages = []

        async def source():
            yield b": heartbeat\n\n"
            entered.set()
            await asyncio.Event().wait()

        stream, clients = upstream(monkeypatch, source)
        monkeypatch.setattr(shared_auth, "call", lambda *_: {"account_id": "same-session"})
        monkeypatch.setattr(shared_auth, "SESSION_EVENTS_MAX_SECONDS", 120 if disconnect else 0.03)
        scope = {"type": "http", "method": "GET", "path": "/api/auth/session-events",
            "headers": [(b"cookie", b"zoj_sso=existing-session")],
            "client": ("127.0.0.1", 0), "asgi": {"spec_version": "2.3"}}

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        response = await auth.watch_shared_session(Request(scope, receive))
        task = asyncio.create_task(response(scope, receive, send))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if disconnect:
                gone.set()
            await asyncio.wait_for(task, 1)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert stream.closed and stream.iterator_closed and clients[0].is_closed
        start = next(message for message in messages if message["type"] == "http.response.start")
        assert start["status"] == 200
        assert not any(key.lower() == b"set-cookie" for key, _ in start["headers"])
        body = b"".join(message.get("body", b"") for message in messages)
        assert body == b": heartbeat\n\n" and b"session_revoked" not in body

    asyncio.run(exercise())
