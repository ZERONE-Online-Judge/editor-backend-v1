"""Deployment drain finishes in-flight work and pauses new claims."""
import threading
from app import worker


def test_drain_pauses_claims_and_marks_idle_worker_ready(monkeypatch, tmp_path):
    marker = tmp_path / "drained"
    stop, drain = threading.Event(), threading.Event()
    drain.set()
    monkeypatch.setattr(worker, "stop_event", stop)
    monkeypatch.setattr(worker, "drain_event", drain)
    monkeypatch.setattr(worker, "DRAIN_MARKER", marker)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "maintenance", lambda: None)
    def unexpected_claim():
        raise AssertionError("A draining worker must leave queued jobs untouched")
    monkeypatch.setattr(worker, "claim_job", unexpected_claim)
    original_wait = stop.wait
    def wait(timeout):
        assert marker.is_file()
        stop.set()
        return original_wait(0)
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()
    assert marker.is_file()
