#!/usr/bin/env bash
# Run on a disposable Linux Docker host, for example a GitHub-hosted runner.
# Only generated test containers/networks/files are used; no production state.
set -euo pipefail
umask 077

deploy_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
for command in docker openssl python3; do
    command -v "$command" >/dev/null || { echo "Required command: $command" >&2; exit 1; }
done
docker compose version >/dev/null
scratch=$(mktemp -d "${TMPDIR:-/tmp}/editor-blue-green-smoke.XXXXXX")
export EDITOR_SMOKE_ROOT=$scratch
export EDITOR_SMOKE_NGINX_IMAGE=${EDITOR_SMOKE_NGINX_IMAGE:-nginx:alpine}
export EDITOR_SMOKE_PYTHON_IMAGE=${EDITOR_SMOKE_PYTHON_IMAGE:-python:3.13-alpine}
export EDITOR_SMOKE_BLUE_API_SHA=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
export EDITOR_SMOKE_GREEN_API_SHA=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
export EDITOR_SMOKE_BLUE_FRONT_SHA=cccccccccccccccccccccccccccccccccccccccc
export EDITOR_SMOKE_GREEN_FRONT_SHA=dddddddddddddddddddddddddddddddddddddddd
# The real route probe must never fall back to its production origin.
export EDITOR_DEPLOY_PUBLIC_ORIGIN=http://10.10.10.2
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}10.10.10.2,frontend,internal-gateway,127.0.0.1,localhost"
export no_proxy=$NO_PROXY
export EDITOR_CONNECTION_DRAIN_TIMEOUT=40
export EDITOR_HEALTH_ATTEMPTS=15
project="editor-blue-green-smoke-$$-${RANDOM}"
compose=(docker compose --project-name "$project" -f "$scratch/compose.yaml")
background=()
cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if (( status != 0 )); then
        "${compose[@]}" ps --all || true
        "${compose[@]}" logs --no-color --tail 100 || true
        for log in "$scratch"/*.log; do
            [[ -f $log ]] || continue
            printf '\n%s\n' "${log##*/}"
            cat "$log"
        done
    fi
    "${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
    for child in "${background[@]}"; do kill "$child" 2>/dev/null || true; done
    rm -rf "$scratch"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$scratch"/{traffic,tls,state,site-blue/assets,site-green/assets,gateway/public,gateway/internal}
traffic_dir="$scratch/traffic"
# Mount directories exactly as production does so rename replaces the inode
# visible inside Nginx, rather than leaving a stale individual-file bind mount.
cp "$deploy_dir/public-gateway.conf.template" "$scratch/gateway/public/default.conf.template"
cp "$deploy_dir/internal-tls.conf" "$scratch/gateway/internal/default.conf"
source "$deploy_dir/traffic-functions.sh"
write_traffic blue blue
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
    -keyout "$scratch/tls/server.key" -out "$scratch/tls/server.crt" \
    -subj '/CN=internal-gateway' \
    -addext 'subjectAltName=IP:10.10.10.118,DNS:internal-gateway' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign' \
    -addext 'extendedKeyUsage=serverAuth' >/dev/null 2>&1
printf '%s\n' "$EDITOR_SMOKE_BLUE_FRONT_SHA" > "$scratch/site-blue/release.txt"
printf '%s\n' "$EDITOR_SMOKE_GREEN_FRONT_SHA" > "$scratch/site-green/release.txt"
printf 'blue asset\n' > "$scratch/site-blue/assets/blue-only.js"
printf 'green asset\n' > "$scratch/site-green/assets/green-only.js"
printf 'blue shared\n' > "$scratch/site-blue/assets/shared.js"
printf 'green shared\n' > "$scratch/site-green/assets/shared.js"
printf 'previous asset must not mask a 500\n' > "$scratch/site-blue/assets/fail.js"
for slot in blue green; do
    printf '%s\n' "$slot frontend" > "$scratch/site-$slot/index.html"
    if [[ $slot == green ]]; then
        failure_rule='return 500;'
    else
        failure_rule='try_files $uri =404;'
    fi
    cat > "$scratch/frontend-$slot.conf" <<EOF
server {
    listen 80;
    root /usr/share/nginx/html;
    location = /healthz { try_files /release.txt =503; }
    location = /assets/fail.js { $failure_rule }
    location /assets/ { try_files \$uri =404; }
    location /api/ {
        proxy_pass http://api-$slot:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$http_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$http_x_forwarded_proto;
        proxy_set_header X-Editor-Request \$http_x_editor_request;
        proxy_buffering off;
    }
    location / { try_files \$uri =404; }
}
EOF
done
# Workers need read access to deliberately generated, nonsecret static fixtures.
chmod -R a+rX "$scratch/site-blue" "$scratch/site-green"
cat > "$scratch/api.py" <<'PY'
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

release = os.environ['SMOKE_RELEASE']
slot = os.environ['SMOKE_SLOT']
state = Path('/state')


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_GET(self):
        parsed = urlsplit(self.path)
        status = 200
        data = {'status': 'ok', 'release': release}
        if parsed.path == '/api/health':
            if (state / ('unhealthy-' + slot)).exists():
                status = 503
                data['status'] = 'error'
        elif parsed.path == '/api/echo':
            data['headers'] = dict(self.headers)
        elif parsed.path != '/api/slow':
            status = 404
        body = json.dumps(data).encode()
        slow = parsed.path == '/api/slow'
        if slow:
            body += b' ' * (512 * 1024)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if slow:
            name = parse_qs(parsed.query)['probe'][0]
            assert name in {'public', 'tls'}
            self.wfile.write(body[:65536])
            self.wfile.flush()
            (state / ('slow-' + name + '.started')).touch()
            time.sleep(10)
            (state / ('slow-' + name + '.released')).touch()
            self.wfile.write(body[65536:])
            self.wfile.flush()
        else:
            self.wfile.write(body)


ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
PY
cat > "$scratch/client.py" <<'PY'
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

blue_api, green_api = 'a' * 40, 'b' * 40
blue_front, green_front = 'c' * 40, 'd' * 40
state = Path('/state')
context = ssl.create_default_context(cafile='/tls/server.crt')
context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))


def read(path, *, tls=False, headers=None):
    origin = 'https://internal-gateway' if tls else 'http://frontend'
    request = urllib.request.Request(origin + path, headers={
        'Connection': 'close', 'Cache-Control': 'no-cache', **(headers or {})})
    with opener.open(request, timeout=25) as response:
        assert response.status == 200
        return response.read()


def status(expected, path, *, tls=False, headers=None):
    try:
        read(path, tls=tls, headers=headers)
    except urllib.error.HTTPError as error:
        assert error.code == expected, (error.code, expected, path)
    else:
        raise AssertionError((expected, path))


mode = sys.argv[1]
if mode == 'allowed':
    headers = {'X-Forwarded-For': '203.0.113.45', 'Host': 'editor.zoj.kr',
               'X-Editor-Request': 'smoke-csrf'}
    public = json.loads(read('/api/echo', headers=headers))['headers']
    assert public['X-Forwarded-For'] == '203.0.113.45', public
    assert public['X-Forwarded-Proto'] == 'https', public
    assert public['Host'] == 'editor.zoj.kr', public
    assert public['X-Editor-Request'] == 'smoke-csrf', public
    internal = json.loads(read('/api/echo', tls=True, headers=headers))['headers']
    assert internal['X-Forwarded-For'] == '10.10.10.200', internal
    with opener.open('https://10.10.10.118/api/health', timeout=5) as response:
        assert json.load(response)['release'] in (blue_api, green_api)
    status(404, '/', tls=True)
    # Both certificate trust and server identity are verified; no insecure mode.
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                'https://internal-gateway/api/health', timeout=5):
            raise AssertionError('An untrusted self-signed certificate was accepted')
    except urllib.error.URLError as error:
        assert isinstance(error.reason, ssl.SSLCertVerificationError), error
    try:
        with socket.create_connection(('internal-gateway', 443), timeout=5) as raw:
            with context.wrap_socket(raw, server_hostname='wrong-editor.test'):
                raise AssertionError('A mismatched TLS identity was accepted')
    except ssl.SSLCertVerificationError:
        pass
elif mode == 'denied':
    forged = {'X-Forwarded-For': '10.10.10.200'}
    status(403, '/api/health', headers=forged)
    status(403, '/release.txt', headers=forged)
    status(403, '/api/health', tls=True, headers=forged)
elif mode == 'assets':
    assert read('/assets/blue-only.js') == b'blue asset\n'
    assert read('/assets/green-only.js') == b'green asset\n'
    assert read('/assets/shared.js') == b'green shared\n'
    status(404, '/assets/missing.js')
    status(500, '/assets/fail.js')
elif mode == 'slow':
    name = sys.argv[2]
    result = json.loads(read('/api/slow?probe=' + name, tls=name == 'tls'))
    assert result['release'] == blue_api, result
    (state / ('slow-' + name + '.client-done')).touch()
elif mode == 'poll':
    counts = {'requests': 0, 'blue': 0, 'green': 0}
    deadline = time.monotonic() + 90
    while not (state / 'poll.stop').exists():
        assert time.monotonic() < deadline, 'Polling controller did not stop'
        for tls in (False, True):
            release = json.loads(read('/api/health', tls=tls))['release']
            assert release in (blue_api, green_api), release
            counts['blue' if release == blue_api else 'green'] += 1
            counts['requests'] += 1
        assert read('/release.txt').decode().strip() in (blue_front, green_front)
        counts['requests'] += 1
        (state / 'poll.json').write_text(json.dumps(counts))
        time.sleep(0.02)
    assert counts['blue'] > 0 and counts['green'] > 0, counts
    assert counts['requests'] >= 30, counts
    print(json.dumps(counts), flush=True)
else:
    raise AssertionError(mode)
PY
cat > "$scratch/compose.yaml" <<'YAML'
services:
  api-blue:
    image: ${EDITOR_SMOKE_PYTHON_IMAGE}
    command: [python, /smoke/api.py]
    environment:
      SMOKE_SLOT: blue
      SMOKE_RELEASE: ${EDITOR_SMOKE_BLUE_API_SHA}
    volumes:
      - ${EDITOR_SMOKE_ROOT}/api.py:/smoke/api.py:ro
      - ${EDITOR_SMOKE_ROOT}/state:/state
    networks: [allowed]
  api-green:
    image: ${EDITOR_SMOKE_PYTHON_IMAGE}
    command: [python, /smoke/api.py]
    environment:
      SMOKE_SLOT: green
      SMOKE_RELEASE: ${EDITOR_SMOKE_GREEN_API_SHA}
    volumes:
      - ${EDITOR_SMOKE_ROOT}/api.py:/smoke/api.py:ro
      - ${EDITOR_SMOKE_ROOT}/state:/state
    networks: [allowed]
  frontend-blue:
    image: ${EDITOR_SMOKE_NGINX_IMAGE}
    depends_on: [api-blue]
    volumes:
      - ${EDITOR_SMOKE_ROOT}/frontend-blue.conf:/etc/nginx/conf.d/default.conf:ro
      - ${EDITOR_SMOKE_ROOT}/site-blue:/usr/share/nginx/html:ro
    networks: [allowed]
  frontend-green:
    image: ${EDITOR_SMOKE_NGINX_IMAGE}
    depends_on: [api-green]
    volumes:
      - ${EDITOR_SMOKE_ROOT}/frontend-green.conf:/etc/nginx/conf.d/default.conf:ro
      - ${EDITOR_SMOKE_ROOT}/site-green:/usr/share/nginx/html:ro
    networks: [allowed]
  frontend:
    image: ${EDITOR_SMOKE_NGINX_IMAGE}
    depends_on: [frontend-blue, frontend-green]
    environment:
      EDITOR_INGRESS_CIDR: 10.10.10.0/24
      NGINX_ENVSUBST_FILTER: EDITOR_
    volumes:
      - ${EDITOR_SMOKE_ROOT}/gateway/public:/etc/nginx/templates:ro
      - ${EDITOR_SMOKE_ROOT}/traffic:/etc/editor-traffic:ro
    networks:
      allowed: {ipv4_address: 10.10.10.2}
      denied: {ipv4_address: 198.18.0.2}
  internal-gateway:
    image: ${EDITOR_SMOKE_NGINX_IMAGE}
    depends_on: [api-blue, api-green, frontend-blue, frontend-green]
    volumes:
      - ${EDITOR_SMOKE_ROOT}/gateway/internal:/etc/nginx/conf.d:ro
      - ${EDITOR_SMOKE_ROOT}/traffic:/etc/editor-traffic:ro
      - ${EDITOR_SMOKE_ROOT}/tls:/tls:ro
    networks:
      allowed: {ipv4_address: 10.10.10.118}
      denied: {ipv4_address: 198.18.0.118}
  allowed-client:
    image: ${EDITOR_SMOKE_PYTHON_IMAGE}
    command: [sleep, infinity]
    volumes:
      - ${EDITOR_SMOKE_ROOT}/client.py:/smoke/client.py:ro
      - ${EDITOR_SMOKE_ROOT}/state:/state
      - ${EDITOR_SMOKE_ROOT}/tls/server.crt:/tls/server.crt:ro
    networks:
      allowed: {ipv4_address: 10.10.10.200}
  denied-client:
    image: ${EDITOR_SMOKE_PYTHON_IMAGE}
    command: [sleep, infinity]
    volumes:
      - ${EDITOR_SMOKE_ROOT}/client.py:/smoke/client.py:ro
      - ${EDITOR_SMOKE_ROOT}/state:/state
      - ${EDITOR_SMOKE_ROOT}/tls/server.crt:/tls/server.crt:ro
    networks:
      denied: {ipv4_address: 198.18.0.200}
networks:
  allowed:
    ipam:
      config: [{subnet: 10.10.10.0/24, ip_range: 10.10.10.128/26}]
  denied:
    ipam:
      config: [{subnet: 198.18.0.0/24, ip_range: 198.18.0.128/26}]
YAML
"${compose[@]}" config --quiet
"${compose[@]}" up -d
probe_routes "$EDITOR_SMOKE_BLUE_API_SHA" "$EDITOR_SMOKE_BLUE_FRONT_SHA"
"${compose[@]}" exec -T allowed-client python /smoke/client.py allowed
"${compose[@]}" exec -T denied-client python /smoke/client.py denied

api=api-green front=frontend-green
backend_sha=$EDITOR_SMOKE_GREEN_API_SHA frontend_sha=$EDITOR_SMOKE_GREEN_FRONT_SHA
expect_failure() {
    local child
    # An asynchronous subshell preserves errexit inside production functions.
    # Calling a function directly inside `if` would disable its intermediate
    # failures, masking an unhealthy API if its frontend happens to be healthy.
    ( set -e; "$@" ) > "$scratch/expected-failure.log" 2>&1 &
    child=$!
    if wait "$child"; then
        echo "Expected failure from $1" >&2
        return 1
    fi
}
cp "$traffic_dir/active.conf" "$scratch/blue.conf"
touch "$scratch/state/unhealthy-green"
expect_failure probe_candidate
cmp "$scratch/blue.conf" "$traffic_dir/active.conf"
probe_routes_once "$EDITOR_SMOKE_BLUE_API_SHA" "$EDITOR_SMOKE_BLUE_FRONT_SHA"
rm "$scratch/state/unhealthy-green"
printf 'wrong revision\n' > "$scratch/site-green/release.txt"
expect_failure probe_candidate
cmp "$scratch/blue.conf" "$traffic_dir/active.conf"
probe_routes_once "$EDITOR_SMOKE_BLUE_API_SHA" "$EDITOR_SMOKE_BLUE_FRONT_SHA"
printf '%s\n' "$frontend_sha" > "$scratch/site-green/release.txt"
chmod a+r "$scratch/site-green/release.txt"
probe_candidate

# Requests run throughout configuration replacement, reload, and rollback.
"${compose[@]}" exec -T allowed-client python /smoke/client.py poll > "$scratch/poll.log" 2>&1 &
poll=$!
background+=("$poll")
for name in public tls; do
    "${compose[@]}" exec -T allowed-client python /smoke/client.py slow "$name" > "$scratch/slow-$name.log" 2>&1 &
    background+=("$!")
done
deadline=$((SECONDS + 20))
until [[ -f "$scratch/state/slow-public.started" && -f "$scratch/state/slow-tls.started" && -s "$scratch/state/poll.json" ]]; do
    [[ $SECONDS -lt $deadline ]] || { echo 'Background HTTP probes did not start.' >&2; exit 1; }
    sleep 0.1
done
old_inode=$(stat -c %i "$traffic_dir/active.conf")
write_traffic green blue
[[ $(stat -c %i "$traffic_dir/active.conf") != "$old_inode" ]]
reload_gateways
(
    wait_gateway_connections
    [[ -f "$scratch/state/slow-public.released" && -f "$scratch/state/slow-tls.released" ]] || {
        echo 'Gateway drain returned before the old responses were released.' >&2
        exit 1
    }
) > "$scratch/drain.log" 2>&1 &
drain=$!
background+=("$drain")
sleep 1
kill -0 "$drain" || { echo 'Gateway drain returned while old slow responses were open.' >&2; exit 1; }
[[ ! -f "$scratch/state/slow-public.client-done" && ! -f "$scratch/state/slow-tls.client-done" ]]
probe_routes "$backend_sha" "$frontend_sha"
"${compose[@]}" exec -T allowed-client python /smoke/client.py assets
for child in "${background[@]:1:2}"; do wait "$child"; done
wait "$drain"
[[ -f "$scratch/state/slow-public.client-done" && -f "$scratch/state/slow-tls.client-done" ]]
background=("$poll")

# A failed nginx -t must leave both live gateways serving the previous route.
cp "$traffic_dir/active.conf" "$scratch/green.conf"
printf 'this is intentionally invalid nginx syntax;\n' > "$traffic_dir/invalid.pending"
chmod 644 "$traffic_dir/invalid.pending"
mv "$traffic_dir/invalid.pending" "$traffic_dir/active.conf"
expect_failure reload_gateways
probe_routes_once "$backend_sha" "$frontend_sha"
cp "$scratch/green.conf" "$traffic_dir/restored.pending"
chmod 644 "$traffic_dir/restored.pending"
mv "$traffic_dir/restored.pending" "$traffic_dir/active.conf"
reload_gateways
for slot in blue green blue green; do
    if [[ $slot == blue ]]; then
        write_traffic blue green
        backend_sha=$EDITOR_SMOKE_BLUE_API_SHA frontend_sha=$EDITOR_SMOKE_BLUE_FRONT_SHA
    else
        write_traffic green blue
        backend_sha=$EDITOR_SMOKE_GREEN_API_SHA frontend_sha=$EDITOR_SMOKE_GREEN_FRONT_SHA
    fi
    reload_gateways
    probe_routes "$backend_sha" "$frontend_sha"
done
wait_gateway_connections
"${compose[@]}" exec -T allowed-client python /smoke/client.py allowed
"${compose[@]}" exec -T denied-client python /smoke/client.py denied
touch "$scratch/state/poll.stop"
wait "$poll"
background=()
cat "$scratch/poll.log"
echo 'Blue-green Docker smoke passed: release routing, failure isolation, atomic reload, in-flight drain, assets, ingress ACL and verified TLS.'
