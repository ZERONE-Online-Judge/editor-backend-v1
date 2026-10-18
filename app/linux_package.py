"""Package the exact Editor engine with standard-library offline adapters."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def environment(sandbox):
    script = '''import json,platform,subprocess
def version(command):
 try:
  result=subprocess.run(command,capture_output=True,text=True,timeout=3)
  return (result.stdout+result.stderr).strip()[:2048]
 except (OSError,subprocess.TimeoutExpired): return None
cpu=None
try:
 for line in open('/proc/cpuinfo'):
  if line.startswith(('model name','Hardware')): cpu=line.split(':',1)[1].strip();break
except OSError: pass
print(json.dumps({'machine':platform.machine(),'kernel':platform.release(),'cpu':cpu,'python':platform.python_version(),'gcc':version(['g++','--version']),'java':version(['java','-version']),'pypy':version(['pypy3','--version']),'kotlin':version(['kotlinc','-version']),'kotlin_jvm':version(['/usr/local/lib/icpc-jdk/bin/java','-version']),'sandbox_cpu_quota':1}))
'''
    result=sandbox.execute(sandbox.new_folder(),['python3','-c',script],cpu_ms=10000,memory_mb=512,output_bytes=16384)
    if result['status']!='ok' or result['exit_code']!=0:
        raise ValueError('재현 패키지의 실제 실행 환경 정보를 확인하지 못했습니다.')
    return json.loads(result['output'])


def add(archive, job, report):
    report=report or {}
    runtime=report.get('preparation_runtime') or report.get('runtime',{})
    metadata={'format':'zoj-linux-replay-v1','revision':job.revision,'document_hash':job.document_hash,
              'runtime':runtime,'measured_environment':report.get('replay_environment'),
              'execution_runs':report.get('execution_runs',[]),'judging_limits':'Editor raw CPU and peak memory; ZOJ contest language corrections are not applied',
              'verified_at_export':bool(report.get('verified',False))}
    archive.writestr('replay/environment.json',json.dumps(metadata,ensure_ascii=False,indent=2))
    archive.writestr('replay/original-results.json',json.dumps({k:report.get(k) for k in ['solutions','test_matrix','verified','unit_tests']},ensure_ascii=False))
    archive.write(ROOT/'replay/replay.py','replay/replay.py')
    archive.write(ROOT/'replay/support.py','replay/engine/support.py')
    archive.writestr('replay/engine/__init__.py','')
    sandbox=(ROOT/'app/sandbox.py').read_text()
    for module in ['config','db','schema','storage']:
        original=f'from .{module} import '
        assert original in sandbox
        sandbox=sandbox.replace(original,'from .support import ')
    archive.writestr('replay/engine/sandbox.py',sandbox)
    archive.write(ROOT/'app/parallel.py','replay/engine/parallel.py')
    archive.write(ROOT/'app/execution_protocol.py','replay/engine/execution_protocol.py')
    archive.write(ROOT/'app/resources.py','replay/engine/resources.py')
    expectations=(ROOT/'app/expectations.py').read_text().replace('from .domain import expanded_tests','from .support import expanded_tests')
    archive.writestr('replay/engine/expectations.py',expectations)
    for name in ['Dockerfile','execute.py','launch.c','install-icpc-runtimes.sh']:
        archive.write(ROOT/'runner'/name,'replay/runtime/'+name)
    for path in (ROOT/'vendor/testlib').iterdir():
        if path.is_file(): archive.write(path,'replay/vendor/testlib/'+path.name)
    archive.writestr('README-LINUX.txt',
        'ZOJ Editor Linux replay package\n\n'
        'Requirements: Python 3.10+ and a working local Docker engine. No API key, database, or Python packages are required.\n'
        'Problem programs execute only in network-disabled Docker sandboxes.\n\n'
        'If the recorded runtime image is present:\n  python3 replay/replay.py\n'
        'Or explicitly select a prepared compatible runtime image:\n  python3 replay/replay.py --image IMAGE_ID\n'
        'A runtime recipe is included. Prepare it while online if needed:\n  docker build -t zoj-editor-runtime:replay replay/runtime\n'
        'For an offline machine, export/load the prepared image with docker save / docker load before replay.\n'
        'The replay command itself never builds or pulls images.\n'
        'Options: --solution NAME (repeatable), --testset NAME, --output PATH.\n'
        'Results default to replay-result.json. Exit 0 means scoped expected verdicts match; 1 means mismatch; 2 means an execution/configuration error.\n\n'
        'All generated tests, answers, sources, validator, checker, scope tags, and original results are included.\n'
        'The frozen Editor engine preserves its CPU, memory, Java, and resource policies. It is not the separate ZOJ contest-judge runtime.\n'
        'Inspect replay/environment.json and compare CPU, architecture, compiler versions, and image IDs before interpreting timing differences.\n'
        'Rebuilding a recipe later does not pin apt packages; use the original image ID/archive for environment parity.\n'
        'Timing varies with hardware and load even with the same image. This package is not a proof of timing equivalence.\n'
        'SHA256SUMS detects accidental file changes; it is not a digital signature.\n')
    hashes=[f"{hashlib.sha256(archive.read(name)).hexdigest()}  {name}" for name in archive.namelist() if not name.endswith('/')]
    archive.writestr('SHA256SUMS','\n'.join(hashes)+'\n')
