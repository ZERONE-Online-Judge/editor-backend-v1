import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "paired_frontend", Path(__file__).parents[1] / "deploy/verify-paired-frontend.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
SHA = "a" * 40


def checked_run():
    return {
        "id": 123, "head_sha": SHA, "head_branch": "main",
        "status": "completed", "conclusion": "success",
        "name": "Editor Frontend CI", "path": ".github/workflows/ci.yml",
        "repository": {"full_name": module.REPOSITORY},
        "event": "workflow_dispatch",
    }


def test_accepts_only_an_exact_successful_frontend_build():
    module.validate_run(SHA, "123", checked_run(), [
        {"name": "build", "status": "completed", "conclusion": "success"},
    ])


@pytest.mark.parametrize("key,value", [
    ("id", 999), ("head_sha", "b" * 40), ("head_branch", "other"),
    ("status", "in_progress"), ("conclusion", "cancelled"),
    ("name", "Unrelated workflow"), ("path", ".github/workflows/unrelated.yml"),
    ("repository", {"full_name": "other/editor-frontend-v1"}),
    ("event", "pull_request"),
])
def test_refuses_other_or_incomplete_release_checks(key, value):
    run = checked_run()
    run[key] = value
    with pytest.raises(ValueError):
        module.validate_run(SHA, "123", run, [
            {"name": "build", "status": "completed", "conclusion": "success"},
        ])


@pytest.mark.parametrize("jobs", [[], [{"name": "build", "status": "completed", "conclusion": "failure"}],
    [{"name": "build", "status": "in_progress", "conclusion": None}],
    [{"name": "build", "status": "completed", "conclusion": "success"}] * 2,
])
def test_refuses_missing_failed_or_ambiguous_builds(jobs):
    with pytest.raises(ValueError):
        module.validate_run(SHA, "123", checked_run(), jobs)


def test_empty_pair_keeps_single_component_releases(monkeypatch, capsys):
    monkeypatch.delenv("PAIRED_FRONTEND_SHA", raising=False)
    monkeypatch.delenv("PAIRED_FRONTEND_RUN", raising=False)
    module.main()
    assert "Single-component" in capsys.readouterr().out


@pytest.mark.parametrize("sha,run_id", [(SHA, ""), ("", "123"), (SHA, "../123"), ("short", "123")])
def test_invalid_pairs_stop_before_network_access(monkeypatch, sha, run_id):
    monkeypatch.setenv("PAIRED_FRONTEND_SHA", sha)
    monkeypatch.setenv("PAIRED_FRONTEND_RUN", run_id)
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("network accessed"))
    with pytest.raises(SystemExit):
        module.main()
