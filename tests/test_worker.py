"""Deployment handover pauses every worker participant and preserves jobs."""
import signal
import threading
import time

import pytest

from app import access_mail, folder_package_exports, worker
from app.db import Job, Problem, SessionLocal


@pytest.fixture
def control(monkeypatch, tmp_path):
    stop, drain, coordinator_idle = (threading.Event() for _ in range(3))
    coordinator_idle.set()
    handlers = {}
    monkeypatch.delenv("EDITOR_WORKER_START_DRAINED", raising=False)
    monkeypatch.setattr(worker, "stop_event", stop)
    monkeypatch.setattr(worker, "drain_event", drain)
    monkeypatch.setattr(worker, "DRAIN_MARKER", tmp_path / "drained")
    monkeypatch.setattr(worker, "READY_MARKER", tmp_path / "ready")
    monkeypatch.setattr(worker, "ACTIVATED_MARKER", tmp_path / "activated")
    monkeypatch.setattr(worker.signal, "signal", lambda sig, handler: handlers.update({sig: handler}))
    monkeypatch.setattr(worker, "maintenance", lambda: None)
    monkeypatch.setattr(access_mail, "run", lambda *args: None)
    monkeypatch.setattr(folder_package_exports, "coordinator_idle", coordinator_idle)
    monkeypatch.setattr(folder_package_exports, "run_coordinator", lambda *args: None)
    return stop, drain, coordinator_idle, handlers


def test_paused_startup_preserves_jobs_and_skips_maintenance(control, monkeypatch, problem):
    stop, drain, _, _ = control
    with SessionLocal() as db:
        source = db.get(Problem, problem)
        job = Job(problem_id=problem, user_id=source.owner_id, kind="verify", document=source.document,
                  document_hash="a" * 64, workspace_version=0, revision=0)
        db.add(job)
        db.commit()
        ident = job.id
    monkeypatch.setenv("EDITOR_WORKER_START_DRAINED", "1")
    original_claim = worker.claim_job
    def unexpected_work():
        raise AssertionError("A paused candidate cannot claim jobs or perform cleanup")
    monkeypatch.setattr(worker, "claim_job", unexpected_work)
    monkeypatch.setattr(worker, "maintenance", unexpected_work)
    def wait(_):
        assert drain.is_set()
        assert worker.DRAIN_MARKER.is_file() and worker.READY_MARKER.is_file()
        with SessionLocal() as db:
            saved = db.get(Job, ident)
            assert saved.status == "queued" and saved.lease is None
        stop.set()
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()
    assert not worker.READY_MARKER.exists() and not worker.DRAIN_MARKER.exists()
    # The exact durable row remains claimable by the next active worker.
    claimed = original_claim()
    assert claimed.id == ident and claimed.status == "running" and claimed.lease


def test_drain_waits_for_folder_coordinator_transaction(control, monkeypatch):
    stop, drain, idle, _ = control
    drain.set()
    idle.clear()
    def wait(_):
        assert worker.READY_MARKER.is_file()
        if not idle.is_set():
            assert not worker.DRAIN_MARKER.exists()
            idle.set()
        else:
            assert worker.DRAIN_MARKER.is_file()
            stop.set()
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()


def test_drain_waits_for_mail_relay_to_finish(control, monkeypatch):
    stop, drain, _, _ = control
    started, release, completed = (threading.Event() for _ in range(3))
    deadline = time.monotonic() + 5
    def mail(_stop, _drain, idle):
        idle.clear()
        started.set()
        try:
            assert release.wait(5)
        finally:
            idle.set()
            completed.set()
    monkeypatch.setattr(access_mail, "run", mail)
    original_wait = stop.wait
    def wait(_):
        assert time.monotonic() < deadline
        assert started.wait(1)
        drain.set()
        if not completed.is_set():
            assert not worker.DRAIN_MARKER.exists()
            release.set()
        elif worker.DRAIN_MARKER.exists():
            stop.set()
        return original_wait(0.01)
    monkeypatch.setattr(stop, "wait", wait)
    try:
        worker.main()
    finally:
        release.set()


def test_drain_finishes_running_job_without_cancellation(control, monkeypatch):
    stop, drain, _, _ = control
    started, release, completed = (threading.Event() for _ in range(3))
    jobs = iter([object()])
    monkeypatch.setattr(worker, "claim_job", lambda: next(jobs, None))
    def process(_):
        started.set()
        drain.set()
        assert release.wait(5)
        assert not stop.is_set()
        completed.set()
    monkeypatch.setattr(worker, "process_job", process)
    original_wait = stop.wait
    deadline = time.monotonic() + 5
    def wait(_):
        assert time.monotonic() < deadline
        assert started.wait(1)
        if not completed.is_set():
            assert not worker.DRAIN_MARKER.exists()
            release.set()
        elif worker.DRAIN_MARKER.exists():
            stop.set()
        return original_wait(0.01)
    monkeypatch.setattr(stop, "wait", wait)
    try:
        worker.main()
    finally:
        release.set()


def test_resume_clears_drain_marker_and_restarts_scheduling(control, monkeypatch):
    stop, drain, _, handlers = control
    drain.set()
    calls = []
    monkeypatch.setattr(worker, "maintenance", lambda: calls.append("maintenance"))
    def claim():
        assert not drain.is_set() and not worker.DRAIN_MARKER.exists()
        calls.append("claim")
        stop.set()
    monkeypatch.setattr(worker, "claim_job", claim)
    def wait(_):
        if drain.is_set():
            assert worker.DRAIN_MARKER.exists() and worker.READY_MARKER.exists()
            assert calls == []
            handlers[signal.SIGUSR2]()
            assert not worker.DRAIN_MARKER.exists()
            assert worker.ACTIVATED_MARKER.exists()
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()
    assert calls == ["maintenance", "claim"]


def test_activated_container_restart_resumes_while_new_candidate_starts_paused(control, monkeypatch):
    stop, drain, _, _ = control
    monkeypatch.setenv("EDITOR_WORKER_START_DRAINED", "1")
    worker.ACTIVATED_MARKER.touch()
    def claim():
        assert not drain.is_set() and not worker.DRAIN_MARKER.exists()
        stop.set()
    monkeypatch.setattr(worker, "claim_job", claim)
    worker.main()
    assert worker.ACTIVATED_MARKER.exists()
    # A newly created candidate has a fresh writable layer and no activation
    # intent, so it still honors START_DRAINED despite using the same image.
    worker.ACTIVATED_MARKER.unlink()
    stop.clear()
    def wait(_):
        assert drain.is_set() and worker.DRAIN_MARKER.exists()
        stop.set()
    monkeypatch.setattr(stop, "wait", wait)
    worker.main()


def test_resume_during_marker_creation_cannot_leave_stale_drain(control, monkeypatch):
    stop, drain, _, handlers = control
    drain.set()
    original_touch = type(worker.DRAIN_MARKER).touch
    def touch(path, *args, **kwargs):
        if path == worker.DRAIN_MARKER:
            handlers[signal.SIGUSR2]()
        return original_touch(path, *args, **kwargs)
    monkeypatch.setattr(type(worker.DRAIN_MARKER), "touch", touch)
    def claim():
        assert not worker.DRAIN_MARKER.exists()
        stop.set()
    monkeypatch.setattr(worker, "claim_job", claim)
    worker.main()


def test_failed_scheduler_removes_readiness_and_drain_markers(control, monkeypatch):
    def fail():
        assert worker.READY_MARKER.exists()
        raise RuntimeError("scheduler failed")
    monkeypatch.setattr(worker, "claim_job", fail)
    with pytest.raises(RuntimeError, match="scheduler failed"):
        worker.main()
    assert not worker.READY_MARKER.exists() and not worker.DRAIN_MARKER.exists()
