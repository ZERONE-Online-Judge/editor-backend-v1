#!/usr/bin/env bash
set -euo pipefail
root=/srv/zoj-editor
cd /srv/editor-source/editor_backend_v1/deploy
exec 9>"$root/config/deploy.lock"
flock -w 600 9
worker=$(sed -n 's/^EDITOR_WORKER_SERVICE=//p' "$root/config/release.env")
worker=${worker:-worker}
[[ $worker == worker || $worker == worker-blue || $worker == worker-green ]] || exit 1
docker compose --project-name zoj-editor --env-file "$root/config/editor.env" \
    --env-file "$root/config/release.env" -f compose.yaml \
    exec -T "$worker" python -m app.judge_smoke --temporary-fixture "$@"
