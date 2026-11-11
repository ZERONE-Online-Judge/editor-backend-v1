"""Exercise the real locked Bash coordinator against stateful Docker/Git fakes."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

import pytest

from app.deployment import schema_plan

ROOT = Path(__file__).resolve().parents[1]
OLD = "a" * 40
NEW = "b" * 40
FRONT = "c" * 40
OTHER = "d" * 40

GIT = r'''#!/usr/bin/env python3
import os,sys
args=sys.argv[1:]; repo=args[1]; args=args[2:]
part='backend' if repo.endswith('backend_v1') else 'frontend'
if args[:3] == ['remote','get-url','origin']:
 print('https://github.com/ZERONE-Online-Judge/editor-'+part+'-v1.git')
elif args[:2] == ['rev-parse','origin/main']:
 print(os.environ['NEW_SHA'] if part==os.environ['COMPONENT'] else os.environ['OTHER_SHA'])
elif args[:2] == ['show',os.environ['OLD_SHA']+':deploy/compose.yaml']:
 print('services:\n  frontend:\n    image: ${EDITOR_FRONTEND_IMAGE}\n  worker:\n    image: ${EDITOR_API_IMAGE}\n')
elif args[0]=='show': print('server { location / { proxy_pass http://api:8000; } }')
'''

DOCKER = r'''#!/usr/bin/env python3
import json,os,pathlib,re,sys
root=pathlib.Path(os.environ['EDITOR_DEPLOY_ROOT']); args=sys.argv[1:]
statepath=root/'state.json'; state=json.loads(statepath.read_text())
with (root/'commands.jsonl').open('a') as f: f.write(json.dumps({'args':args,'api_image':os.getenv('EDITOR_API_IMAGE'),'frontend_image':os.getenv('EDITOR_FRONTEND_IMAGE')})+'\n')
if args[0]!='compose': sys.exit(0)
commands={'config','build','run','up','exec','kill','stop','start','restart','ps'}
i=next(i for i,a in enumerate(args) if a in commands); command=args[i]; rest=args[i+1:]
services=[a for a in rest if re.fullmatch(r'(api|frontend|worker)(-(blue|green))?|internal-gateway|db',a)]
service=services[0] if services else ''
fail=os.getenv('FAIL_STAGE','')
def save(): statepath.write_text(json.dumps(state))
def sha(service):
 color=service.rsplit('-',1)[-1].upper()
 return os.getenv('EDITOR_'+color+'_API_IMAGE','').split(':')[-1]
if command=='build' and fail=='build': sys.exit(1)
if command=='run':
 if 'app.deployment' in rest:
  if fail=='schema': sys.exit(1)
  print('revision revision current')
 sys.exit(0)
if command=='ps':
 if fail=='inspect' and service==os.getenv('OLD_WORKER','worker-blue'): sys.exit(1)
 if state.get(service,{}).get('running'): print(service+'-container')
elif command=='up':
 if fail=='candidate' and service.startswith('api-'): sys.exit(1)
 for s in services:
  state[s]={'running':True,'drained':s.startswith('worker-')}
 save()
elif command in ('start','restart'):
 state.setdefault(service,{})['running']=True
 if command=='restart': state[service]['drained']=False
 save()
elif command=='stop':
 for s in services: state.setdefault(s,{})['running']=False
 save()
elif command=='kill':
 if 'SIGUSR1' in rest:
  if fail=='drain' and service==os.getenv('OLD_WORKER','worker-blue'): sys.exit(1)
  state.setdefault(service,{})['drained']=True
 if 'SIGUSR2' in rest: state.setdefault(service,{})['drained']=False
 save()
elif command=='exec':
 if 'nginx' in rest and '-t' in rest and service=='internal-gateway' and fail=='nginx':
  if 'api-green' in (root/'config/traffic/active.conf').read_text(): sys.exit(1)
 if 'test' in rest and '/tmp/editor-worker-drained' in rest:
  sys.exit(0 if state.get(service,{}).get('drained') else 1)
 if 'python' in rest:
  code=rest[rest.index('-c')+1] if '-c' in rest else ''
  if 'not Path' in code:
   sys.exit(0 if not state.get(service,{}).get('drained') else 1)
  if 'urllib.request' in code and service.startswith('api-'):
   sys.exit(0 if rest[-1]==sha(service) else 1)
 if 'wget' in rest:
  url=rest[-1]
  if '/healthz' in url: sys.exit(0)
  if ':8081/' in url:
   traffic=(root/'config/traffic/active.conf').read_text()
   color=re.search(r'api-(blue|green)',traffic)[1]
   backend=os.getenv('EDITOR_'+color.upper()+'_API_IMAGE','').split(':')[-1]
   frontend=os.getenv('EDITOR_'+color.upper()+'_FRONTEND_IMAGE','').split(':')[-1]
  else:
   frontend=os.getenv('EDITOR_'+service.rsplit('-',1)[-1].upper()+'_FRONTEND_IMAGE','').split(':')[-1]
  if '/api/health' in url: print(json.dumps({'status':'ok','release':backend}))
  if '/release.txt' in url: print(frontend)
'''


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / "server"
    source = tmp_path / "sources"
    (root / "config/traffic").mkdir(parents=True)
    (root / "config/editor.env").write_text('EDITOR_DB_PASSWORD=private-test-value\n')
    (root / "state.json").write_text(json.dumps({"worker-blue": {"running": True, "drained": False}}))
    release = "\n".join([
        f"EDITOR_API_IMAGE=zoj-editor-api:{OLD}", f"EDITOR_FRONTEND_IMAGE=zoj-editor-frontend:{FRONT}",
        f"EDITOR_RUNNER_IMAGE=zoj-editor-runtime:{OLD}", f"EDITOR_RENDER_IMAGE=zoj-editor-renderer:{OLD}",
        f"EDITOR_RUNTIME_REVISION=editor-runtime-{OLD}", "EDITOR_DEPLOY_MODE=blue-green",
        "EDITOR_ACTIVE_SLOT=blue", "EDITOR_WORKER_SERVICE=worker-blue", "EDITOR_SCHEMA_REVISION=revision",
        f"EDITOR_BLUE_API_IMAGE=zoj-editor-api:{OLD}", f"EDITOR_BLUE_FRONTEND_IMAGE=zoj-editor-frontend:{FRONT}",
        f"EDITOR_BLUE_BACKEND_SHA={OLD}", f"EDITOR_BLUE_WORKER_IMAGE=zoj-editor-api:{OLD}",
    ]) + "\n"
    (root / "config/release.env").write_text(release)
    traffic = "upstream editor_frontend { server frontend-blue:80; }\nupstream editor_api { server api-blue:8000; }\n"
    (root / "config/traffic/active.conf").write_text(traffic)
    shutil.copytree(ROOT / "deploy", source / "editor_backend_v1/deploy")
    (source / "editor_frontend_v1").mkdir()
    binary = tmp_path / "bin"
    binary.mkdir()
    for name, content in [("git", GIT), ("docker", DOCKER), ("sleep", "#!/bin/sh\nexit 0\n")]:
        path = binary / name
        path.write_text(content)
        path.chmod(0o755)
    context = SimpleNamespace(root=root, source=source, release=release, traffic=traffic, component="backend", fail="", public_fail=False, backend_by_color={"blue": OLD, "green": NEW}, frontend_by_color={"blue": FRONT, "green": FRONT})
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            selected = "api-green" in (root / "config/traffic/active.conf").read_text()
            color = "green" if selected else "blue"
            backend = context.backend_by_color[color]
            frontend = context.frontend_by_color[color]
            if context.public_fail and selected:
                self.send_error(503)
                return
            data = json.dumps({"status": "ok", "release": backend}).encode() if self.path.startswith('/api/') else (frontend+'\n').encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def run(component="backend", fail="", public_fail=False, **changes):
        context.component = component
        context.public_fail = public_fail
        if component != 'rollback':
            current = dict(line.split('=', 1) for line in (root/'config/release.env').read_text().splitlines())
            candidate = 'green' if current.get('EDITOR_ACTIVE_SLOT') == 'blue' else 'blue'
            context.backend_by_color[candidate] = NEW if component == 'backend' else current['EDITOR_API_IMAGE'].split(':')[1]
            context.frontend_by_color[candidate] = NEW if component == 'frontend' else current['EDITOR_FRONTEND_IMAGE'].split(':')[1]
        env = {**os.environ, "PATH": str(binary)+os.pathsep+os.environ['PATH'], "EDITOR_DEPLOY_ROOT": str(root),
               "EDITOR_SOURCE_ROOT": str(source), "COMPONENT": component, "OLD_SHA": OLD, "NEW_SHA": NEW,
               "OTHER_SHA": OTHER, "FAIL_STAGE": fail, "EDITOR_HEALTH_ATTEMPTS": "1",
               "EDITOR_DEPLOY_PUBLIC_ORIGIN": f"http://127.0.0.1:{server.server_port}", **changes}
        return subprocess.run(["bash", str(source/'editor_backend_v1/deploy/deploy-main.sh'), component, NEW], env=env, text=True, capture_output=True, timeout=20)
    context.run = run
    context.commands = lambda: [json.loads(line) for line in (root/'commands.jsonl').read_text().splitlines()]
    yield context
    server.shutdown()
    server.server_close()
    thread.join()


def commands_of(deployment, command):
    return [row['args'] for row in deployment.commands() if command in row['args']]


def test_backend_switch_keeps_live_api_and_pins_healthy_partner(deployment):
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    release = (deployment.root/'config/release.env').read_text()
    assert f"EDITOR_API_IMAGE=zoj-editor-api:{NEW}" in release
    assert f"EDITOR_FRONTEND_IMAGE=zoj-editor-frontend:{FRONT}" in release
    assert 'EDITOR_WORKER_SERVICE=worker-green' in release
    assert 'EDITOR_ACTIVE_SLOT=green' in release
    assert (deployment.root/'config/release.previous.env').read_text() == deployment.release
    assert not any('api-blue' in args for args in commands_of(deployment, 'stop'))
    assert not commands_of(deployment, 'restart')
    kills = commands_of(deployment, 'kill')
    assert ['SIGUSR1', 'worker-blue'] == kills[0][-2:]
    assert ['SIGUSR2', 'worker-green'] == kills[-1][-2:]
    runs = commands_of(deployment, 'run')
    assert any('app.auth_preflight' in args for args in runs)
    assert any('app.execution_preflight' in args for args in runs)


@pytest.mark.parametrize('stage', ['build', 'schema', 'candidate'])
def test_candidate_failure_never_drains_or_changes_healthy_release(deployment, stage):
    result = deployment.run(fail=stage)
    assert result.returncode != 0
    assert (deployment.root/'config/release.env').read_text() == deployment.release
    assert (deployment.root/'config/traffic/active.conf').read_text() == deployment.traffic
    assert not commands_of(deployment, 'kill')
    assert not commands_of(deployment, 'stop')


def test_failed_public_health_restores_traffic_worker_and_manifest(deployment):
    result = deployment.run(public_fail=True)
    assert result.returncode != 0
    assert (deployment.root/'config/release.env').read_text() == deployment.release
    assert (deployment.root/'config/traffic/active.conf').read_text() == deployment.traffic
    state = json.loads((deployment.root/'state.json').read_text())
    assert state['worker-blue'] == {'running': True, 'drained': False}
    assert not state['worker-green']['running']
    assert not any('api-blue' in args for args in commands_of(deployment, 'stop'))


def test_frontend_switch_never_restarts_or_drains_worker(deployment):
    result = deployment.run(component='frontend')
    assert result.returncode == 0, result.stderr
    assert not commands_of(deployment, 'kill')
    assert not commands_of(deployment, 'stop')
    assert not any(any(arg.startswith('worker-') for arg in args) for args in commands_of(deployment, 'up'))
    release = (deployment.root/'config/release.env').read_text()
    assert f'EDITOR_API_IMAGE=zoj-editor-api:{OLD}' in release
    assert 'EDITOR_WORKER_SERVICE=worker-blue' in release


def test_frontend_only_skips_backend_dependencies_but_checks_schema(deployment):
    result = deployment.run(component='frontend')
    assert result.returncode == 0, result.stderr
    runs = commands_of(deployment, 'run')
    assert not any('app.execution_preflight' in args for args in runs)
    assert not any('app.auth_preflight' in args for args in runs)
    assert any('app.deployment' in args for args in runs)
    assert any('python' in args and '-c' in args for args in runs)


def test_failed_old_worker_drain_resumes_without_stopping_active_jobs(deployment):
    result = deployment.run(fail='drain')
    assert result.returncode != 0
    assert not any('worker-blue' in args for args in commands_of(deployment, 'stop'))
    assert not commands_of(deployment, 'restart')
    assert any(args[-2:] == ['SIGUSR2', 'worker-blue'] for args in commands_of(deployment, 'kill'))
    assert (deployment.root/'config/release.env').read_text() == deployment.release


def test_worker_color_is_independent_after_frontend_only_switch(deployment):
    release = deployment.release.replace('EDITOR_ACTIVE_SLOT=blue', 'EDITOR_ACTIVE_SLOT=green')
    release += f'EDITOR_GREEN_API_IMAGE=zoj-editor-api:{OLD}\nEDITOR_GREEN_FRONTEND_IMAGE=zoj-editor-frontend:{FRONT}\nEDITOR_GREEN_BACKEND_SHA={OLD}\n'
    (deployment.root/'config/release.env').write_text(release)
    (deployment.root/'config/traffic/active.conf').write_text(deployment.traffic.replace('blue', 'green'))
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    assert any(args[-1] == 'worker-green' for args in commands_of(deployment, 'up'))
    assert not any('--force-recreate' in args and args[-1] == 'worker-blue' for args in commands_of(deployment, 'up'))


def test_online_schema_policy_requires_exact_reviewed_file(tmp_path):
    path = tmp_path/'migration.py'
    path.write_text('expand_only = True\n')
    revision = SimpleNamespace(path=path, revision='new')
    scripts = SimpleNamespace(get_heads=lambda: ['new'], iterate_revisions=lambda target, old: [revision])
    with pytest.raises(RuntimeError, match='compatibility review'):
        schema_plan(scripts, ['old'])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert schema_plan(scripts, ['old'], {'new': digest}) == ('old', 'new', True)
    path.write_text('destructive = True\n')
    with pytest.raises(RuntimeError, match='compatibility review'):
        schema_plan(scripts, ['old'], {'new': digest})


def test_schema_policy_rejects_uninitialized_or_branched_database():
    scripts = SimpleNamespace(get_heads=lambda: ['new'])
    for heads in ([], ['one', 'two']):
        with pytest.raises(RuntimeError, match='initialized'):
            schema_plan(scripts, heads)


def test_repeated_manual_rollback_keeps_both_slot_images_and_history(deployment):
    result = deployment.run()
    assert result.returncode == 0, result.stderr
    result = deployment.run(component='rollback')
    assert result.returncode == 0, result.stderr
    release = (deployment.root/'config/release.env').read_text()
    assert 'EDITOR_ACTIVE_SLOT=blue' in release
    assert f'EDITOR_GREEN_API_IMAGE=zoj-editor-api:{NEW}' in release
    result = deployment.run(component='rollback')
    assert result.returncode == 0, result.stderr
    assert 'EDITOR_ACTIVE_SLOT=green' in (deployment.root/'config/release.env').read_text()
    assert not any('fetch' in row['args'] for row in deployment.commands())


def test_failed_switch_preserves_prior_rollback_target(deployment):
    history = 'known-healthy-history\n'
    (deployment.root/'config/release.previous.env').write_text(history)
    result = deployment.run(public_fail=True)
    assert result.returncode != 0
    assert (deployment.root/'config/release.previous.env').read_text() == history


def test_failed_recovery_blocks_next_deployment(deployment):
    (deployment.root/'config/recovery-required').write_text('candidate worker needs reconciliation\n')
    result = deployment.run()
    assert result.returncode != 0
    assert 'reconciliation' in result.stderr
    assert not (deployment.root/'commands.jsonl').exists()


def test_failed_worker_inspection_never_activates_replacement(deployment):
    result = deployment.run(fail='inspect')
    assert result.returncode != 0
    assert not any(args[-2:] == ['SIGUSR2', 'worker-green'] for args in commands_of(deployment, 'kill'))
    assert not any('worker-blue' in args for args in commands_of(deployment, 'stop'))
    assert (deployment.root/'config/release.env').read_text() == deployment.release


def test_failed_nginx_validation_restores_before_reloading(deployment):
    result = deployment.run(fail='nginx')
    assert result.returncode != 0
    assert (deployment.root/'config/traffic/active.conf').read_text() == deployment.traffic
    assert (deployment.root/'config/release.env').read_text() == deployment.release
    assert not (deployment.root/'config/recovery-required').exists()


def test_malformed_blue_green_metadata_fails_closed(deployment):
    (deployment.root/'config/release.env').write_text(deployment.release.replace('EDITOR_ACTIVE_SLOT=blue\n', ''))
    result = deployment.run()
    assert result.returncode != 0
    assert 'Incomplete' in result.stderr
    assert not (deployment.root/'commands.jsonl').exists()
