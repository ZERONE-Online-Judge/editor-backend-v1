from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from app import judge_requests


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(judge_requests.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(judge_requests.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now + seconds))
    monkeypatch.setattr(judge_requests.get_settings(), 'judge_request_retry_seconds', 120)
    return clock


@pytest.mark.parametrize('status', [429, 502, 503, 504])
def test_transient_errors_retry_same_request_beyond_three_attempts(clock, status):
    calls, notices = [], []
    content = object()
    def remote(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) <= 4:
            raise HTTPException(status, {'code': 'api_busy'})
        return {'id': 'same-operation'}
    result = judge_requests.request_with_retry(remote, 'POST', '/runs', content, check=lambda: None,
                                               on_wait=lambda: notices.append(True))
    assert result == {'id': 'same-operation'}
    assert len(calls) == 5 and all(call == calls[0] for call in calls)
    assert notices == [True] and clock.now >= 12


@pytest.mark.parametrize('status', [400, 401, 403, 404, 409, 422, 500])
def test_permanent_errors_are_not_retried(clock, status):
    calls = []
    def remote():
        calls.append(True)
        raise HTTPException(status)
    with pytest.raises(HTTPException) as error:
        judge_requests.request_with_retry(remote, check=lambda: None)
    assert error.value.status_code == status and calls == [True] and clock.now == 0


def test_retries_stop_at_the_request_budget(clock, monkeypatch):
    monkeypatch.setattr(judge_requests.get_settings(), 'judge_request_retry_seconds', 2)
    calls = []
    def remote():
        calls.append(clock.now)
        raise HTTPException(503)
    with pytest.raises(HTTPException):
        judge_requests.request_with_retry(remote, check=lambda: None)
    assert calls == [0, 1] and clock.now == 2


def test_cancellation_interrupts_backoff_without_another_request(clock):
    calls = []
    def remote():
        calls.append(True)
        raise HTTPException(503)
    def check():
        if clock.now >= .2:
            raise RuntimeError('cancelled')
    with pytest.raises(RuntimeError, match='cancelled'):
        judge_requests.request_with_retry(remote, check=check)
    assert calls == [True] and clock.now < 1
