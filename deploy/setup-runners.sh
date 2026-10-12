#!/usr/bin/env bash
# Run with sudo on the new Editor VM, never on the existing ZOJ server.
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { echo 'Run this installer with sudo.' >&2; exit 1; }
[[ $(uname -m) == x86_64 ]] || { echo 'This installer requires Linux x86_64.' >&2; exit 1; }
token_dir=${1:?Pass the private directory containing backend.token and frontend.token}
[[ -f "$token_dir/backend.token" && -f "$token_dir/frontend.token" ]] || exit 1
runner_version=2.338.0
runner_sha=af4b794c1bc41d73d40535e3fe092a39f9679cd8d965954c2aca25a05ca41d32
install_user=editor-runner
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"; rm -f "$token_dir/backend.token" "$token_dir/frontend.token"' EXIT
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends docker.io docker-compose-v2 git curl ca-certificates jq libicu74
systemctl enable --now docker
id "$install_user" >/dev/null 2>&1 || useradd --system --create-home --home-dir /srv/zoj-editor/runner-home --shell /usr/sbin/nologin "$install_user"
usermod -aG docker "$install_user"
install -d -m 0755 /srv/zoj-editor
install -d -m 0750 -o "$install_user" -g "$install_user" /srv/editor-source /srv/zoj-editor/runners /srv/zoj-editor/config /srv/zoj-editor/backups
install -d -m 0750 -o 1000 -g 1000 /srv/zoj-editor/data
install -d -m 0750 /srv/zoj-editor/postgres /srv/zoj-editor/certs
curl -fSL --retry 3 "https://github.com/actions/runner/releases/download/v${runner_version}/actions-runner-linux-x64-${runner_version}.tar.gz" -o "$scratch/runner.tar.gz"
echo "$runner_sha  $scratch/runner.tar.gz" | sha256sum -c -
for component in backend frontend; do
    runner_dir="/srv/zoj-editor/runners/$component"
    install -d -m 0750 -o "$install_user" -g "$install_user" "$runner_dir"
    if [[ ! -f "$runner_dir/.runner" ]]; then
        tar -xzf "$scratch/runner.tar.gz" -C "$runner_dir"
        chown -R "$install_user:$install_user" "$runner_dir"
        install -m 0600 -o "$install_user" -g "$install_user" "$token_dir/$component.token" "$runner_dir/setup.token"
        runuser -u "$install_user" -- bash -c '
            set -euo pipefail
            cd "$1"
            token=$(cat setup.token)
            trap "rm -f setup.token" EXIT
            ./config.sh --unattended --url "https://github.com/ZERONE-Online-Judge/editor-$2-v1" \
                --token "$token" --name "zoj-editor-$2" --labels "zoj-editor,editor-$2" --work _work
        ' -- "$runner_dir" "$component"
    else
        expected="https://github.com/ZERONE-Online-Judge/editor-$component-v1"
        [[ $(jq -r .gitHubUrl "$runner_dir/.runner") == "$expected" ]] || { echo 'Existing runner belongs to a different repository.' >&2; exit 1; }
    fi
    cd "$runner_dir"
    [[ -f .service ]] || ./svc.sh install "$install_user"
    ./svc.sh start
    chmod 0600 .credentials .credentials_rsaparams .runner
    source_dir="/srv/editor-source/editor_${component}_v1"
    if [[ ! -e "$source_dir" ]]; then
        runuser -u "$install_user" -- git clone "https://github.com/ZERONE-Online-Judge/editor-$component-v1.git" "$source_dir"
    fi
done
env_file=/srv/zoj-editor/config/editor.env
if [[ ! -e "$env_file" ]]; then
    python3 - "$env_file" "$(stat -c %g /var/run/docker.sock)" <<'PY'
import os, secrets, sys
path, gid = sys.argv[1:]
with open(path, 'x') as f:
    f.write('EDITOR_DB_PASSWORD=' + secrets.token_hex(32) + '\n')
    f.write('EDITOR_SESSION_SECRET=' + secrets.token_hex(32) + '\n')
    f.write('EDITOR_DOCKER_GID=' + gid + '\n')
    f.write('EDITOR_MAIL_MODE=zoj\nEDITOR_MAIL_RELAY_URL=https://zoj.kr/api\nEDITOR_MAIL_RELAY_SECRET=\nEDITOR_MAIL_RELAY_CA_FILE=\n')
    f.write('EDITOR_MAIL_FROM=ZOJ Editor <noreply@editor.zoj.kr>\n')
    f.write('EDITOR_BIND_ADDRESS=127.0.0.1\nEDITOR_INGRESS_CIDR=127.0.0.1/32\nEDITOR_HTTP_PORT=8090\n')
    f.write('EDITOR_STORAGE_ROOT=/srv/zoj-editor\nEDITOR_WORKER_CONCURRENCY=2\n')
    f.write('EDITOR_ZOJ_API_URL=\nEDITOR_ZOJ_PUBLIC_ORIGIN=https://zoj.kr\nEDITOR_ZOJ_BRIDGE_SECRET=\n')
os.chmod(path, 0o600)
PY
    chown "$install_user:$install_user" "$env_file"
fi
echo 'Both repository runners are installed. ZOJ mail API and ingress configuration are still required before production deployment.'
