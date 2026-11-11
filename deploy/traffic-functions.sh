#!/usr/bin/env bash
# Sourced by the locked coordinator. No private environment values are printed.
write_traffic() {
    local target=$1 temporary previous_frontend
    [[ $target == blue || $target == green ]] || return 1
    temporary=$(mktemp "$traffic_dir/active.pending.XXXXXX")
    previous_frontend=${2:-${old_slot:-$target}}
    [[ $previous_frontend != legacy ]] || previous_frontend=$target
    [[ $previous_frontend == blue || $previous_frontend == green ]] || return 1
    printf 'upstream editor_frontend { server frontend-%s:80; keepalive 16; }\nupstream editor_api { server api-%s:8000; keepalive 16; }\nupstream editor_previous_frontend { server frontend-%s:80; keepalive 16; }\n' "$target" "$target" "$previous_frontend" > "$temporary"
    # Nginx is unprivileged in some images; upstream names contain no secrets.
    chmod 644 "$temporary"
    mv "$temporary" "$traffic_dir/active.conf"
}
wait_gateway_connections() {
    local deadline=$((SECONDS + ${EDITOR_CONNECTION_DRAIN_TIMEOUT:-1900})) service
    for service in frontend internal-gateway; do
        until "${compose[@]}" exec -T "$service" sh -c '
            for name in /proc/[0-9]*/comm; do
                [ "$(cat "$name" 2>/dev/null)" = nginx ] || continue
                grep -qa "shutting down" "${name%comm}cmdline" && exit 1
            done
            exit 0'; do
            [[ $SECONDS -lt $deadline ]] || { echo 'Previous gateway connections are still draining; retain both release slots.' >&2; return 1; }
            sleep 3
        done
    done
}
stage_gateway_configs() {
    mkdir -p "$root/config/gateway/public" "$root/config/gateway/internal"
    cp "$script_dir/public-gateway.conf.template" "$root/config/gateway/public/default.conf.pending"
    cp "$script_dir/internal-tls.conf" "$root/config/gateway/internal/default.conf.pending"
    chmod 644 "$root/config/gateway/"{public,internal}/default.conf.pending
    mv "$root/config/gateway/public/default.conf.pending" "$root/config/gateway/public/default.conf.template"
    mv "$root/config/gateway/internal/default.conf.pending" "$root/config/gateway/internal/default.conf"
}
restore_gateway_configs() {
    local kind filename
    for kind in public internal; do
        filename=default.conf
        [[ $kind != public ]] || filename=default.conf.template
        if [[ -f "$gateway_backup/$kind/$filename" ]]; then
            cp "$gateway_backup/$kind/$filename" "$root/config/gateway/$kind/restore.pending" || return 1
            mv "$root/config/gateway/$kind/restore.pending" "$root/config/gateway/$kind/$filename" || return 1
        fi
    done
}
reload_gateways() {
    # The template directory is mounted, so atomically replaced templates are
    # visible across releases. Re-render without replacing the live container.
    "${compose[@]}" exec -T frontend sh /docker-entrypoint.d/20-envsubst-on-templates.sh || return 1
    "${compose[@]}" exec -T frontend nginx -t || return 1
    "${compose[@]}" exec -T internal-gateway nginx -t || return 1
    "${compose[@]}" exec -T frontend nginx -s reload || return 1
    "${compose[@]}" exec -T internal-gateway nginx -s reload || return 1
}
drain_worker() {
    local service=$1 deadline=$((SECONDS + ${EDITOR_DRAIN_TIMEOUT:-1900}))
    # Check support before sending a signal that would terminate an old process.
    "${compose[@]}" exec -T "$service" python -c 'from app.worker import drain_event' >/dev/null || return 1
    "${compose[@]}" kill -s SIGUSR1 "$service" || return 1
    until "${compose[@]}" exec -T "$service" test -f /tmp/editor-worker-drained; do
        [[ $SECONDS -lt $deadline ]] || { echo 'Worker drain timed out; active work was not terminated.' >&2; return 1; }
        sleep 3
    done
}
wait_worker_active() {
    local service=$1 deadline=$((SECONDS + 60))
    until "${compose[@]}" exec -T "$service" python -c "import time; from pathlib import Path; p=Path('/tmp/editor-worker-ready'); assert p.is_file() and time.time()-p.stat().st_mtime < 60; assert not Path('/tmp/editor-worker-drained').exists()"; do
        [[ $SECONDS -lt $deadline ]] || return 1
        sleep 2
    done
}
probe_candidate() {
    "${compose[@]}" exec -T "$api" python -c 'import json,sys,urllib.request; data=json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/health",timeout=5)); assert data["status"] == "ok"; assert data.get("release") == sys.argv[1]' "$backend_sha"
    "${compose[@]}" exec -T "$front" wget -q -O /dev/null http://127.0.0.1/healthz
    [[ $("${compose[@]}" exec -T "$front" wget -q -O - http://127.0.0.1/release.txt) == "$frontend_sha" ]]
}
probe_routes_once() {
    local expected_backend=$1 expected_frontend=$2 response
    response=$("${compose[@]}" exec -T frontend wget -q -O - http://127.0.0.1:8081/api/health) || return 1
    python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["status"] == "ok" and d.get("release") == sys.argv[2]' "$response" "$expected_backend" || return 1
    [[ $("${compose[@]}" exec -T frontend wget -q -O - http://127.0.0.1:8081/release.txt) == "$expected_frontend" ]] || return 1
    response=$("${compose[@]}" exec -T internal-gateway wget -q -O - http://127.0.0.1:8081/api/health) || return 1
    python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["status"] == "ok" and d.get("release") == sys.argv[2]' "$response" "$expected_backend" || return 1
    python3 - "$expected_backend" "$expected_frontend" "${EDITOR_DEPLOY_PUBLIC_ORIGIN:-https://editor.zoj.kr}" <<'PY'
import json, sys, urllib.request
backend, frontend, origin = sys.argv[1:]
def read(path):
    request = urllib.request.Request(origin.rstrip('/') + path, headers={'Connection': 'close', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()
assert json.loads(read('/api/health?deploy=' + backend)).get('release') == backend
assert read('/release.txt?deploy=' + frontend).decode().strip() == frontend
PY
}
probe_routes() {
    local attempt
    for attempt in $(seq 1 "${EDITOR_HEALTH_ATTEMPTS:-10}"); do
        if probe_routes_once "$1" "$2"; then return 0; fi
        sleep 3
    done
    echo 'Selected release failed public/internal route health checks.' >&2
    return 1
}
probe_legacy_routes() {
    local certificate endpoint
    "${legacy_compose[@]}" exec -T internal-gateway nginx -t || return 1
    certificate=$("${legacy_compose[@]}" exec -T internal-gateway cat /tls/server.crt) || return 1
    endpoint=$("${legacy_compose[@]}" port internal-gateway 443) || return 1
    python3 - "$old_frontend_sha" "${EDITOR_DEPLOY_PUBLIC_ORIGIN:-https://editor.zoj.kr}" "$endpoint" "$certificate" <<'PYTHON'
import json, ssl, sys, urllib.request
frontend, origin, endpoint, certificate = sys.argv[1:]
with urllib.request.urlopen(origin.rstrip('/') + '/api/health', timeout=10) as response:
    assert json.load(response)['status'] == 'ok'
with urllib.request.urlopen(origin.rstrip('/') + '/release.txt?deploy=' + frontend, timeout=10) as response:
    assert response.read().decode().strip() == frontend
context = ssl.create_default_context(cadata=certificate)
context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
with urllib.request.urlopen('https://' + endpoint + '/api/health', context=context, timeout=10) as response:
    assert json.load(response)['status'] == 'ok'
PYTHON
}
restore_previous() {
    local recovery=0 candidate_worker_id
    if [[ -f "$previous" ]]; then
        cp "$previous" "$root/config/release.restore.pending" || return 1
        mv "$root/config/release.restore.pending" "$release_file" || return 1
    fi
    if (( had_previous_history )); then
        cp "$previous_history" "$root/config/release.history.pending" || return 1
        mv "$root/config/release.history.pending" "$root/config/release.previous.env" || return 1
    else
        rm -f "$root/config/release.previous.env"
    fi
    git -C "$backend_repo" checkout --detach "$old_backend_sha" || recovery=1
    git -C "$frontend_repo" checkout --detach "$old_frontend_sha" || recovery=1
    if (( traffic_changed )); then
        if [[ $old_slot == legacy ]]; then
            "${legacy_compose[@]}" up -d --no-deps --force-recreate --wait --wait-timeout 60 frontend internal-gateway || recovery=1
            probe_legacy_routes || recovery=1
        else
            restore_gateway_configs || recovery=1
            cp "$old_traffic" "$traffic_dir/restore.pending" || recovery=1
            chmod 644 "$traffic_dir/restore.pending" || recovery=1
            mv "$traffic_dir/restore.pending" "$traffic_dir/active.conf" || recovery=1
            reload_gateways || recovery=1
            probe_routes "$old_backend_sha" "$old_frontend_sha" || recovery=1
        fi
    fi
    if (( worker_started )); then
        # Never run old and replacement side-effect consumers concurrently.
        candidate_worker_id=$("${compose[@]}" ps -q --status running "$candidate_worker") || return 1
        if [[ -n "$candidate_worker_id" ]]; then
            drain_worker "$candidate_worker" || return 1
            "${compose[@]}" stop -t 30 "$candidate_worker" || return 1
        fi
    fi
    if (( old_worker_drain_requested && ! old_worker_drained )); then
        if [[ $old_worker == worker ]]; then
            # A pre-blue/green worker cannot resume with SIGUSR2. Wait for its
            # original jobs to finish before a safe restart, never cancel them.
            drain_worker "$old_worker" || return 1
            old_worker_drained=1
        else
            "${compose[@]}" kill -s SIGUSR2 "$old_worker" || recovery=1
            return "$recovery"
        fi
    fi
    if (( old_worker_drained )); then
        # Restart also resumes legacy workers lacking SIGUSR2. The old consumer
        # was drained before stop; no queued/running job status is rewritten.
        if [[ $old_worker == worker ]]; then
            "${legacy_compose[@]}" up -d --no-deps worker || recovery=1
            "${legacy_compose[@]}" restart -t 30 worker || recovery=1
        else
            "${compose[@]}" start "$old_worker" || recovery=1
            "${compose[@]}" kill -s SIGUSR2 "$old_worker" || recovery=1
            wait_worker_active "$old_worker" || recovery=1
        fi
    fi
    return "$recovery"
}
write_release() {
    local destination=$1 target=$2 worker_service=$3 schema=$4 temporary key
    temporary=$(mktemp "$root/config/release.env.XXXXXX")
    {
        for key in EDITOR_API_IMAGE EDITOR_FRONTEND_IMAGE EDITOR_RUNNER_IMAGE EDITOR_RENDER_IMAGE EDITOR_RUNTIME_REVISION EDITOR_BLUE_API_IMAGE EDITOR_BLUE_FRONTEND_IMAGE EDITOR_BLUE_BACKEND_SHA EDITOR_GREEN_API_IMAGE EDITOR_GREEN_FRONTEND_IMAGE EDITOR_GREEN_BACKEND_SHA EDITOR_BLUE_WORKER_IMAGE EDITOR_GREEN_WORKER_IMAGE; do
            [[ -z ${!key:-} ]] || printf '%s=%s\n' "$key" "${!key}"
        done
        printf 'EDITOR_DEPLOY_MODE=blue-green\nEDITOR_ACTIVE_SLOT=%s\nEDITOR_WORKER_SERVICE=%s\nEDITOR_SCHEMA_REVISION=%s\n' "$target" "$worker_service" "$schema"
    } > "$temporary"
    mv "$temporary" "$destination"
}
rollback_release() {
    local target_file=$1 target_slot target_worker target_backend target_frontend plan before target action key value switch_worker=0
    target_slot=$(release_value EDITOR_ACTIVE_SLOT "$target_file")
    target_worker=$(release_value EDITOR_WORKER_SERVICE "$target_file")
    target_backend=$(release_value EDITOR_API_IMAGE "$target_file")
    target_frontend=$(release_value EDITOR_FRONTEND_IMAGE "$target_file")
    [[ $target_slot == blue || $target_slot == green ]] || { echo 'Legacy port migration needs the saved legacy Compose file for rollback.' >&2; return 1; }
    [[ $target_backend =~ ^zoj-editor-api:([a-f0-9]{40})$ ]] || return 1
    backend_sha=${BASH_REMATCH[1]}
    [[ $target_frontend =~ ^zoj-editor-frontend:([a-f0-9]{40})$ ]] || return 1
    frontend_sha=${BASH_REMATCH[1]}
    [[ $target_worker == worker || $target_worker == worker-blue || $target_worker == worker-green ]] || return 1
    export EDITOR_API_IMAGE="$target_backend" EDITOR_FRONTEND_IMAGE="$target_frontend"
    export "EDITOR_${target_slot^^}_API_IMAGE=$target_backend" "EDITOR_${target_slot^^}_FRONTEND_IMAGE=$target_frontend" "EDITOR_${target_slot^^}_BACKEND_SHA=$backend_sha"
    for key in EDITOR_RUNNER_IMAGE EDITOR_RENDER_IMAGE EDITOR_RUNTIME_REVISION; do
        value=$(release_value "$key" "$target_file")
        [[ $value =~ ^[a-zA-Z0-9:._/-]+$ ]] || return 1
        export "$key=$value"
    done
    api="api-$target_slot" front="frontend-$target_slot"
    # An old image must understand the current schema before any route change.
    plan=$("${compose[@]}" run --rm --no-deps "$api" python -m app.deployment)
    read -r before target action <<< "$plan"
    [[ $action == current ]] || { echo 'Rollback requires schema compatibility review; no downgrade was attempted.' >&2; return 1; }
    probe_candidate
    worker_started=0 old_worker_drain_requested=0 old_worker_drained=0 old_worker_stopped=0 traffic_changed=0 committed=0
    candidate_worker=$target_worker
    if [[ $target_worker != "$old_worker" ]]; then
        switch_worker=1
        old_worker_drain_requested=1
        drain_worker "$old_worker"
        old_worker_drained=1
        "${compose[@]}" stop -t 30 "$old_worker"
        old_worker_stopped=1
        worker_started=1
        "${compose[@]}" start "$target_worker"
    fi
    traffic_changed=1
    write_traffic "$target_slot"
    reload_gateways
    probe_routes "$backend_sha" "$frontend_sha"
    if (( switch_worker )); then
        "${compose[@]}" kill -s SIGUSR2 "$target_worker"
        wait_worker_active "$target_worker"
    fi
    # Merge both retained slot image maps so a rollback can roll forward again.
    cp "$previous" "$root/config/release.previous.env.pending"
    mv "$root/config/release.previous.env.pending" "$root/config/release.previous.env"
    write_release "$release_file" "$target_slot" "$target_worker" "$before"
    git -C "$backend_repo" checkout --detach "$backend_sha"
    git -C "$frontend_repo" checkout --detach "$frontend_sha"
    committed=1
    echo "Editor rolled back ($target_slot): backend $backend_sha / frontend $frontend_sha"
}
