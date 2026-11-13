"""Verify an explicit, fully checked frontend before a paired cutover."""
import json
import os
import re
import urllib.request

REPOSITORY = "ZERONE-Online-Judge/editor-frontend-v1"


def validate_run(sha, run_id, run, jobs):
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("A complete frontend SHA and CI run are required together.")
    expected = {
        "id": int(run_id), "head_sha": sha, "head_branch": "main",
        "status": "completed", "conclusion": "success",
        "name": "Editor Frontend CI", "path": ".github/workflows/ci.yml",
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError("The frontend CI must have passed for this exact main SHA.")
    if run.get("repository", {}).get("full_name") != REPOSITORY:
        raise ValueError("The frontend CI belongs to a different repository.")
    if run.get("event") not in {"push", "workflow_dispatch"}:
        raise ValueError("Only a main release check can authorize a paired release.")
    builds = [job for job in jobs if job.get("name") == "build"]
    if len(builds) != 1 or builds[0].get("status") != "completed" or builds[0].get("conclusion") != "success":
        raise ValueError("The frontend build and container checks must pass.")


def main():
    sha = os.environ.get("PAIRED_FRONTEND_SHA", "")
    run_id = os.environ.get("PAIRED_FRONTEND_RUN", "")
    if not sha and not run_id:
        print("Single-component release; no paired frontend requested.")
        return
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise SystemExit("A complete frontend SHA and CI run are required together.")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "zoj-editor-release"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]

    def read(suffix):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{run_id}{suffix}",
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    try:
        run = read("")
        jobs = read("/jobs?per_page=100")
        validate_run(sha, run_id, run, jobs.get("jobs", []))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("Unable to verify frontend CI; paired release stopped.") from None
    print(f"Verified frontend {sha} with successful CI run {run_id}.")


if __name__ == "__main__":
    main()
