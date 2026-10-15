from app.config import get_settings
from app.db import Job, SessionLocal


def test_environment_reports_current_settings_and_real_queue_counts(owner, problem, monkeypatch):
    settings = get_settings()
    for name, value in {"test_parallelism": 7, "execution_concurrency": 5, "worker_concurrency": 4, "judge_concurrency": 2, "job_timeout_seconds": 90, "stress_max_seconds": 600}.items():
        monkeypatch.setattr(settings, name, value)
    first = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0, "reuse": False}).json()
    owner.post(f"/api/problems/{problem}/jobs", json={"kind": "render", "version": 0, "reuse": False})
    with SessionLocal() as db:
        db.get(Job, first["id"]).status = "running"
        db.commit()
    response = owner.get("/api/execution-environment")
    assert response.status_code == 200
    env = response.json()
    assert env["limits"]["test_parallelism"] == 7
    assert env["limits"]["execution_concurrency"] == 5
    assert env["limits"]["worker_concurrency"] == 4
    assert env["limits"]["judge_concurrency"] == 2
    assert env["limits"]["job_timeout_seconds"] == env["limits"]["stress_seconds"] == 90
    assert env["limits"]["stress_iterations"] == env["limits"]["tests_per_set"] == 1000
    assert env["limits"]["generator_arguments"] == 64
    assert env["limits"]["generator_cpu_ms"] == 10000
    assert env["capacity"] == {"running_jobs": 1, "queued_jobs": 1}
    assert env["updated_at"] > 0
    assert "session_secret" not in str(env) and "bridge_secret" not in str(env)
