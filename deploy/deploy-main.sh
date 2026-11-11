#!/usr/bin/env bash
# Both repository runners use this coordinator and the backup/deployment lock.
set -Eeuo pipefail
umask 077
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
component=${1:?backend, frontend or rollback}
event_sha=${2:-}
[[ $component == backend || $component == frontend || $component == rollback ]] || exit 2
[[ $component == rollback || $event_sha =~ ^[a-f0-9]{40}$ ]] || exit 2
root=${EDITOR_DEPLOY_ROOT:-/srv/zoj-editor}
source_root=${EDITOR_SOURCE_ROOT:-/srv/editor-source}
env_file="$root/config/editor.env"
release_file="$root/config/release.env"
[[ -r "$env_file" && -r "$release_file" ]] || { echo 'An existing healthy Editor release is required for blue/green deployment.' >&2; exit 1; }
exec 9>"$root/config/deploy.lock"
flock -w 2400 9
[[ ! -f "$root/config/recovery-required" ]] || { echo 'A previous recovery needs reconciliation; inspect recovery-required before another deployment.' >&2; exit 1; }
# Values are data, never shell source. Keep compatibility with existing backups.
release_value() { sed -n "s/^$1=//p" "${2:-$release_file}"; }
load_release() {
    local key value
    while IFS='=' read -r key value; do
        [[ -n "$key" ]] || continue
        [[ $key =~ ^EDITOR_[A-Z_]+$ && $value =~ ^[a-zA-Z0-9:._/-]+$ ]] || { echo 'Invalid release metadata.' >&2; return 1; }
        export "$key=$value"
    done < "$1"
}
load_release "$release_file"
old_backend=$(release_value EDITOR_API_IMAGE)
old_frontend=$(release_value EDITOR_FRONTEND_IMAGE)
[[ $old_backend =~ ^zoj-editor-api:([a-f0-9]{40})$ ]] || exit 1
old_backend_sha=${BASH_REMATCH[1]}
[[ $old_frontend =~ ^zoj-editor-frontend:([a-f0-9]{40})$ ]] || exit 1
old_frontend_sha=${BASH_REMATCH[1]}
old_slot=${EDITOR_ACTIVE_SLOT:-legacy}
old_worker=${EDITOR_WORKER_SERVICE:-worker}
[[ $old_slot == legacy || $old_slot == blue || $old_slot == green ]] || exit 1
[[ $old_worker == worker || $old_worker == worker-blue || $old_worker == worker-green ]] || exit 1
mode=${EDITOR_DEPLOY_MODE:-legacy}
if [[ $mode == blue-green ]]; then
    [[ ( $old_slot == blue || $old_slot == green ) && ( $old_worker == worker-blue || $old_worker == worker-green ) ]] || { echo 'Incomplete blue/green release metadata.' >&2; exit 1; }
else
    [[ $mode == legacy && $old_slot == legacy && $old_worker == worker ]] || exit 1
fi
backend_repo="$source_root/editor_backend_v1"
frontend_repo="$source_root/editor_frontend_v1"
for part in backend frontend; do
    repo="$source_root/editor_${part}_v1"
    [[ $(git -C "$repo" remote get-url origin) == "https://github.com/ZERONE-Online-Judge/editor-$part-v1.git" ]] || exit 1
    [[ -z $(git -C "$repo" status --porcelain) ]] || { echo "Uncommitted server changes in $part; deployment stopped." >&2; exit 1; }
    if [[ $component != rollback ]]; then
        git -C "$repo" fetch --prune origin
        git -C "$repo" rev-parse --verify refs/remotes/origin/main >/dev/null
    fi
done
if [[ $component != rollback ]]; then
    target_repo="$source_root/editor_${component}_v1"
    if [[ $(git -C "$target_repo" rev-parse origin/main) != "$event_sha" ]]; then
        echo 'Skip a superseded deployment; a newer main commit exists.'
        exit 0
    fi
fi
backend_sha=$old_backend_sha
frontend_sha=$old_frontend_sha
[[ $component != backend ]] || backend_sha=$event_sha
[[ $component != frontend ]] || frontend_sha=$event_sha
# Save the known healthy metadata before changing checkouts or runtime state.
previous=$(mktemp "$root/config/release.pending.XXXXXX")
cp "$release_file" "$previous"
previous_history=$(mktemp "$root/config/release.history.XXXXXX")
had_previous_history=0
if [[ -f "$root/config/release.previous.env" ]]; then
    cp "$root/config/release.previous.env" "$previous_history"
    had_previous_history=1
fi
gateway_backup=$(mktemp -d "$root/config/gateway.previous.XXXXXX")
for kind in public internal; do
    [[ ! -d "$root/config/gateway/$kind" ]] || cp -a "$root/config/gateway/$kind" "$gateway_backup/$kind"
done
traffic_dir="$root/config/traffic"
mkdir -p "$traffic_dir" "$root/backups"
old_traffic=$(mktemp "$traffic_dir/previous.XXXXXX")
[[ ! -f "$traffic_dir/active.conf" ]] || cp "$traffic_dir/active.conf" "$old_traffic"
legacy_dir="$root/config/legacy"
if [[ $old_slot == legacy ]]; then
    mkdir -p "$legacy_dir"
    git -C "$backend_repo" show "$old_backend_sha:deploy/compose.yaml" > "$legacy_dir/compose.yaml"
    git -C "$backend_repo" show "$old_backend_sha:deploy/internal-tls.conf" > "$legacy_dir/internal-tls.conf"
    # Preserve the old bind mount when the rollback Compose file moves directories.
    python3 - "$legacy_dir/compose.yaml" "$legacy_dir/internal-tls.conf" <<'PY'
import pathlib, sys
path = pathlib.Path(sys.argv[1])
path.write_text(path.read_text().replace('./internal-tls.conf:', sys.argv[2] + ':'))
PY
fi
# Freeze this tested Compose definition for the whole transaction. Recovery may
# restore a legacy source checkout which does not yet define the slot services.
compose_snapshot=$(mktemp "$root/config/compose.pending.XXXXXX")
cp "$script_dir/compose.yaml" "$compose_snapshot"
export EDITOR_STORAGE_ROOT="$root"
export EDITOR_API_IMAGE="zoj-editor-api:$backend_sha"
export EDITOR_FRONTEND_IMAGE="zoj-editor-frontend:$frontend_sha"
export EDITOR_BACKEND_SHA="$backend_sha" EDITOR_FRONTEND_SHA="$frontend_sha"
export EDITOR_LEGACY_API_IMAGE="$old_backend"
compose=(docker compose --project-name zoj-editor --project-directory "$backend_repo/deploy" --env-file "$env_file" -f "$compose_snapshot")
legacy_compose=(env "EDITOR_API_IMAGE=$old_backend" "EDITOR_FRONTEND_IMAGE=$old_frontend"
    "EDITOR_RUNNER_IMAGE=$(release_value EDITOR_RUNNER_IMAGE "$previous")"
    "EDITOR_RENDER_IMAGE=$(release_value EDITOR_RENDER_IMAGE "$previous")"
    "EDITOR_RUNTIME_REVISION=$(release_value EDITOR_RUNTIME_REVISION "$previous")"
    docker compose --project-name zoj-editor --env-file "$env_file" --env-file "$previous" -f "$legacy_dir/compose.yaml")
# Source the implementation from this workflow's tested checkout, even when a
# frontend release pins an older backend checkout.
source "$script_dir/traffic-functions.sh"
worker_started=0
old_worker_drain_requested=0
old_worker_drained=0
old_worker_stopped=0
traffic_changed=0
committed=0
on_failure() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 && ! committed )); then
        echo 'Deployment failed; restoring the last healthy traffic and worker.' >&2
        if ! restore_previous; then
            printf 'Recovery incomplete; verify traffic and both workers before removing this file.\n' > "$root/config/recovery-required"
            echo 'Automatic recovery needs operator attention; further deployments are blocked. No database downgrade was attempted.' >&2
        fi
    fi
    rm -f "$previous" "$previous_history" "$old_traffic" "$compose_snapshot"
    rm -rf -- "$gateway_backup"
    exit "$status"
}
trap on_failure EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# Runtime sources use exactly the tested component and last healthy partner.
git -C "$backend_repo" checkout --detach "$backend_sha"
git -C "$frontend_repo" checkout --detach "$frontend_sha"
cd "$backend_repo/deploy"
if [[ $component == rollback ]]; then
    [[ $old_slot != legacy && -r "$root/config/release.previous.env" ]] || { echo 'No retained blue/green release is available.' >&2; exit 1; }
    rollback_release "$root/config/release.previous.env"
    rm -f "$previous" "$previous_history" "$old_traffic" "$compose_snapshot"
    rm -rf -- "$gateway_backup"
    exit 0
fi
slot=blue
[[ $old_slot != blue ]] || slot=green
upper=${slot^^}
export "EDITOR_${upper}_API_IMAGE=$EDITOR_API_IMAGE" "EDITOR_${upper}_FRONTEND_IMAGE=$EDITOR_FRONTEND_IMAGE" "EDITOR_${upper}_BACKEND_SHA=$backend_sha"
export EDITOR_RUNNER_IMAGE="zoj-editor-runtime:$backend_sha"
export EDITOR_RENDER_IMAGE="zoj-editor-renderer:$backend_sha"
export EDITOR_RUNTIME_REVISION="editor-runtime-$backend_sha"
api="api-$slot"
front="frontend-$slot"
worker_slot=blue
[[ $old_worker != worker-blue ]] || worker_slot=green
candidate_worker="worker-$worker_slot"
if [[ $component == backend ]]; then
    export "EDITOR_${worker_slot^^}_WORKER_IMAGE=$EDITOR_API_IMAGE"
fi
"${compose[@]}" config --quiet
"${compose[@]}" build "$api" "$front"
"${compose[@]}" run --rm --no-deps "$api" python -c '
from app.config import get_settings
try:
    get_settings()
except Exception:
    raise SystemExit("Editor production environment is invalid; private values were not logged.")
'
if [[ $component == backend ]]; then
    # The unchanged backend is already serving during a frontend-only release.
    # Do not block a UI fix on an unavailable authentication or judging service.
    "${compose[@]}" run --rm --no-deps "$api" python -m app.auth_preflight
    "${compose[@]}" run --rm --no-deps "$api" python -m app.execution_preflight
fi
schema_plan=$("${compose[@]}" run --rm --no-deps "$api" python -m app.deployment)
read -r schema_before schema_target schema_action <<< "$schema_plan"
[[ $schema_before =~ ^[a-zA-Z0-9_]+$ && $schema_target =~ ^[a-zA-Z0-9_]+$ && ( $schema_action == current || $schema_action == upgrade ) ]]
if [[ $component == frontend && $schema_action != current ]]; then
    echo 'A frontend deployment cannot migrate the shared database.' >&2
    false
fi
if [[ $schema_action == upgrade ]]; then
    # Only exact reviewed expand-only revisions reach this point. Live requests
    # and the previous worker remain compatible; do not stop the serving API.
    backup="$root/backups/$(date -u +%Y%m%dT%H%M%SZ)-${backend_sha:0:12}.dump"
    "${compose[@]}" exec -T db pg_dump -U editor -d editor -Fc > "$backup"
    "${compose[@]}" run --rm --no-deps "$api" alembic upgrade head
fi
[[ $old_slot == legacy ]] || wait_gateway_connections
"${compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 180 "$api" "$front"
probe_candidate
bash "$script_dir/install-backup-helper.sh" "$EDITOR_API_IMAGE"
if [[ $component == backend ]]; then
    # The replacement starts paused, so it cannot claim jobs/send mail before
    # the old worker is fully drained. Stopping a busy worker is never allowed.
    worker_started=1
    candidate_worker_id=$("${compose[@]}" ps -q --status running "$candidate_worker")
    if [[ -n "$candidate_worker_id" ]]; then
        drain_worker "$candidate_worker"
        "${compose[@]}" stop -t 30 "$candidate_worker"
    fi
    "${compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 120 "$candidate_worker"
    "${compose[@]}" exec -T "$candidate_worker" test -f /tmp/editor-worker-drained
    old_worker_id=$("${compose[@]}" ps -q --status running "$old_worker")
    if [[ -n "$old_worker_id" ]]; then
        old_worker_drain_requested=1
        drain_worker "$old_worker"
        old_worker_drained=1
        old_worker_stopped=1
        "${compose[@]}" stop -t 30 "$old_worker"
    fi
fi
traffic_changed=1
stage_gateway_configs
write_traffic "$slot"
if [[ $old_slot == legacy ]]; then
    # One-time port ownership migration; later releases only reload Nginx.
    "${compose[@]}" up -d --no-deps --force-recreate frontend internal-gateway
else
    reload_gateways
fi
probe_routes "$backend_sha" "$frontend_sha"
if [[ $component == backend ]]; then
    "${compose[@]}" kill -s SIGUSR2 "$candidate_worker"
    wait_worker_active "$candidate_worker"
    new_worker=$candidate_worker
else
    new_worker=$old_worker
fi
# Repeat after worker handoff. On failure the candidate is drained before the
# previous worker is resumed, preventing repeated external side effects.
probe_routes "$backend_sha" "$frontend_sha"
cp "$previous" "$root/config/release.previous.env.pending"
mv "$root/config/release.previous.env.pending" "$root/config/release.previous.env"
write_release "$release_file" "$slot" "$new_worker" "$schema_target"
committed=1
rm -f "$previous"
trap - EXIT INT TERM
# The next inactive slot is the rollback release. Leave its API/frontend alive;
# a later deployment may recycle it only after its own candidate checks succeed.
rm -f "$previous_history" "$old_traffic" "$compose_snapshot"
rm -rf -- "$gateway_backup"
echo "Editor deployed ($slot): backend $backend_sha / frontend $frontend_sha"
