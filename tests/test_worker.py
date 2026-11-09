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
    control_thread = threading.current_thread()
    def wait(timeout):
        if threading.current_thread() is not control_thread:
            return original_wait(timeout)
        assert marker.is_file()
        stop.set()
        return original_wait(0)
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()
    assert marker.is_file()


def test_drain_waits_for_folder_coordinator_transaction(monkeypatch, tmp_path):
    from app import folder_package_exports
    marker = tmp_path / "drained"
    stop, drain, idle = threading.Event(), threading.Event(), threading.Event()
    drain.set()
    monkeypatch.setattr(worker, "stop_event", stop)
    monkeypatch.setattr(worker, "drain_event", drain)
    monkeypatch.setattr(worker, "DRAIN_MARKER", marker)
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    monkeypatch.setattr(worker, "maintenance", lambda: None)
    monkeypatch.setattr(folder_package_exports, "coordinator_idle", idle)
    monkeypatch.setattr(folder_package_exports, "run_coordinator", lambda *args: None)
    original_wait = stop.wait
    control_thread = threading.current_thread()
    def wait(timeout):
        if threading.current_thread() is not control_thread:
            return original_wait(timeout)
        if not idle.is_set():
            assert not marker.exists()
            idle.set()
        else:
            assert marker.is_file()
            stop.set()
        return original_wait(0)
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()
    assert marker.is_file()
