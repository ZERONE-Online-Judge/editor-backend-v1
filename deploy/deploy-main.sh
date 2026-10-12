#!/usr/bin/env bash
# Both repository runners call this script; the server lock serializes them.
set -euo pipefail
umask 077
component=${1:?backend or frontend}
event_sha=${2:?Full tested commit SHA}
[[ $component == backend || $component == frontend ]] || exit 2
[[ $event_sha =~ ^[a-f0-9]{40}$ ]] || exit 2
root=/srv/zoj-editor
source_root=/srv/editor-source
env_file="$root/config/editor.env"
[[ -r "$env_file" ]] || { echo 'Configure the private Editor environment first.' >&2; exit 1; }
exec 9>"$root/config/deploy.lock"
flock -w 2400 9
for part in backend frontend; do
    repo="$source_root/editor_${part}_v1"
    [[ $(git -C "$repo" remote get-url origin) == "https://github.com/ZERONE-Online-Judge/editor-$part-v1.git" ]] || exit 1
    [[ -z $(git -C "$repo" status --porcelain) ]] || { echo "Uncommitted server changes in $part; deployment stopped." >&2; exit 1; }
    git -C "$repo" fetch --prune origin
    git -C "$repo" rev-parse --verify refs/remotes/origin/main >/dev/null
done
target_repo="$source_root/editor_${component}_v1"
if [[ $(git -C "$target_repo" rev-parse origin/main) != "$event_sha" ]]; then
    echo 'Skip a superseded deployment; a newer main commit exists.'
    exit 0
fi
other=frontend
[[ $component == frontend ]] && other=backend
other_sha=$(git -C "$source_root/editor_${other}_v1" rev-parse origin/main)
if [[ -f "$root/config/release.env" ]]; then
    # Update only the repository that passed this workflow. Keep its companion
    # at the last healthy release rather than using an untested newer main.
    image_key=EDITOR_FRONTEND_IMAGE
    [[ $other == backend ]] && image_key=EDITOR_API_IMAGE
    other_sha=$(sed -n "s/^${image_key}=zoj-editor-[^:]*://p" "$root/config/release.env")
    [[ $other_sha =~ ^[a-f0-9]{40}$ ]] || exit 1
else
    # First release: the companion's hosted validation job must have passed.
    python3 - "$other" "$other_sha" <<'PY'
import json, sys, urllib.request
component, sha = sys.argv[1:]
base = 'https://api.github.com/repos/ZERONE-Online-Judge/editor-' + component + '-v1'
def get(path):
    request = urllib.request.Request(base + path, headers={'User-Agent':'ZOJ-Editor-Deploy', 'Accept':'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)
runs = get('/actions/workflows/ci.yml/runs?head_sha=' + sha + '&event=push&per_page=10')['workflow_runs']
required = 'test' if component == 'backend' else 'build'
assert any(any(job['name'] == required and job['conclusion'] == 'success'
               for job in get('/actions/runs/' + str(run['id']) + '/jobs')['jobs'])
           for run in runs if run['head_branch'] == 'main'), 'Companion main validation has not passed yet; rerun deployment after its CI passes.'
PY
fi
git -C "$target_repo" checkout --detach "$event_sha"
git -C "$source_root/editor_${other}_v1" checkout --detach "$other_sha"
backend_sha=$(git -C "$source_root/editor_backend_v1" rev-parse HEAD)
frontend_sha=$(git -C "$source_root/editor_frontend_v1" rev-parse HEAD)
export EDITOR_API_IMAGE="zoj-editor-api:$backend_sha"
export EDITOR_FRONTEND_IMAGE="zoj-editor-frontend:$frontend_sha"
export EDITOR_RUNNER_IMAGE="zoj-editor-runtime:$backend_sha"
export EDITOR_RENDER_IMAGE="zoj-editor-renderer:$backend_sha"
export EDITOR_RUNTIME_REVISION="editor-runtime-$backend_sha"
cd "$source_root/editor_backend_v1/deploy"
compose=(docker compose --project-name zoj-editor --env-file "$env_file" -f compose.yaml)
# Validate required configuration without printing the resolved secrets.
"${compose[@]}" config --quiet
"${compose[@]}" --profile images build api frontend runtime renderer
"${compose[@]}" run --rm --no-deps api python -c '
from app.config import get_settings
try:
    get_settings()
except Exception:
    raise SystemExit("Editor production environment is invalid. Review the private configuration; values were not logged.")
'
worker_id=$("${compose[@]}" ps -q --status running worker)
if [[ -n "$worker_id" ]]; then
    "${compose[@]}" exec -T worker python -c 'from app.worker import drain_event' >/dev/null
    "${compose[@]}" kill -s SIGUSR1 worker
    deadline=$((SECONDS + 1900))
    until "${compose[@]}" exec -T worker test -f /tmp/editor-worker-drained; do
        [[ $SECONDS -lt $deadline ]] || { echo 'Worker drain timed out; leave the current service running and review its jobs.' >&2; exit 1; }
        sleep 3
    done
fi
"${compose[@]}" stop -t 30 api worker
"${compose[@]}" up -d --wait --wait-timeout 120 db
backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-${backend_sha:0:12}.dump"
"${compose[@]}" exec -T db pg_dump -U editor -d editor -Fc > "$backup"
# The old images remain available. A migration/health failure is explicit;
# never automatically overwrite or restore a live database.
"${compose[@]}" run --rm --no-deps api alembic upgrade head
"${compose[@]}" up -d --wait --wait-timeout 180 api worker frontend
"${compose[@]}" exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=5)"
release=$(mktemp "$root/config/release.env.XXXXXX")
printf 'EDITOR_API_IMAGE=%s\nEDITOR_FRONTEND_IMAGE=%s\nEDITOR_RUNNER_IMAGE=%s\nEDITOR_RENDER_IMAGE=%s\nEDITOR_RUNTIME_REVISION=%s\n' \
    "$EDITOR_API_IMAGE" "$EDITOR_FRONTEND_IMAGE" "$EDITOR_RUNNER_IMAGE" "$EDITOR_RENDER_IMAGE" "$EDITOR_RUNTIME_REVISION" > "$release"
mv "$release" "$root/config/release.env"
echo "Editor deployed: backend $backend_sha / frontend $frontend_sha"
