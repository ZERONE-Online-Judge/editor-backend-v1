#!/usr/bin/env bash
# Install only the shared account bridge and private certificates on zoj-editor.
set -euo pipefail
umask 077
[[ $(id -u) == 0 && $(hostname) == zoj-editor ]] || exit 1
stage=${1:?Private staging directory}
[[ -f "$stage/editor-auth.env" && -f "$stage/zoj-ca.crt" && -f "$stage/server.crt" && -f "$stage/server.key" ]] || exit 1
install -d -m 0755 /srv/zoj-editor/certs
install -d -m 0750 /srv/zoj-editor/tls
install -m 0644 "$stage/zoj-ca.crt" /srv/zoj-editor/certs/zoj-ca.crt
install -m 0644 "$stage/server.crt" /srv/zoj-editor/tls/server.crt
install -m 0600 "$stage/server.key" /srv/zoj-editor/tls/server.key
python3 - "$stage/editor-auth.env" <<'PY'
import json, pathlib, re, shutil, sys, time, os
source=pathlib.Path(sys.argv[1])
secret=dict(line.split('=',1) for line in source.read_text().splitlines() if '=' in line)
assert set(secret)=={'EDITOR_AUTH_BRIDGE_SECRET','EDITOR_ZOJ_BRIDGE_SECRET'}
assert all(len(v)>=32 and '\n' not in v for v in secret.values())
path=pathlib.Path('/srv/zoj-editor/config/editor.env')
assert path.is_file()
backup=path.with_name('editor.env.before-shared-'+str(int(time.time())))
shutil.copy2(path,backup);os.chmod(backup,0o600)
updates={**secret,'EDITOR_AUTH_API_URL':'https://10.10.10.110:6443/api','EDITOR_AUTH_CA_FILE':'/certs/zoj-ca.crt',
 'EDITOR_ZOJ_API_URL':'https://10.10.10.110:6443/api','EDITOR_ZOJ_CA_FILE':'/certs/zoj-ca.crt','EDITOR_ZOJ_PUBLIC_ORIGIN':'https://zoj.kr',
 'EDITOR_BIND_ADDRESS':'10.10.10.118','EDITOR_HTTP_PORT':'8090','EDITOR_INGRESS_CIDR':'10.10.10.0/24'}
lines=[]
for line in path.read_text().splitlines():
 key=line.split('=',1)[0] if '=' in line and not line.startswith('#') else ''
 if key in updates or key.startswith(('EDITOR_SMTP_','EDITOR_MAIL_RELAY_')) or key=='EDITOR_MAIL_MODE':continue
 lines.append(line)
lines.extend(k+'='+json.dumps(v) for k,v in updates.items())
temporary=path.with_suffix('.new');temporary.write_text('\n'.join(lines)+'\n');os.chmod(temporary,0o600)
import pwd
owner=pwd.getpwnam('editor-runner');os.chown(temporary,owner.pw_uid,owner.pw_gid);os.replace(temporary,path)
print('Shared authentication configured; private values were not logged.')
PY
rm -f "$stage/editor-auth.env" "$stage/server.key"
echo 'Ready: HTTP 10.10.10.118:8090 / private API TLS 10.10.10.118:9443'
