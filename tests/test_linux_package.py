import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import pytest
from app.db import SessionLocal, Problem, Revision
from app.demo import populate
from app.config import get_settings
from app.packages import build_package
from app.worker import claim_job, process_job


def package(problem):
    with SessionLocal() as db:
        row=db.get(Problem,problem)
        doc=populate(db,row)
        db.commit()
    return SimpleNamespace(id='f'*32,problem_id=problem,revision=0,document=doc,document_hash='a'*64,
                           options={'export_format':'linux','verify':False}),{'tests':list(doc['testsets']['tests']['tests'].values())}


def test_linux_package_freezes_engine_sources_tests_and_environment_without_python_dependencies(problem,tmp_path):
    job,sets=package(problem)
    artifact=build_package(job,sets,{'runtime':{'image_id':'sha256:'+'a'*64},'verified':False})
    assert artifact['format']=='linux' and artifact['filename'].endswith('-linux.zip')
    with zipfile.ZipFile(get_settings().data_dir/'packages'/(job.id+'.zip')) as archive:
        assert 'replay/runtime/launch.c' in archive.namelist()
        assert 'replay/engine/support.py' in archive.namelist()
        assert 'replay/engine/parallel.py' in archive.namelist()
        assert b'from .config' not in archive.read('replay/engine/sandbox.py')
        assert b'from .support import get_settings' in archive.read('replay/engine/sandbox.py')
        assert b'from .domain' not in archive.read('replay/engine/expectations.py')
        for name in ['replay/engine/sandbox.py','replay/replay.py','replay/engine/expectations.py','replay/engine/parallel.py']:
            compile(archive.read(name),name,'exec')
        hashes=archive.read('SHA256SUMS').decode().splitlines()
        for line in hashes:
            digest,name=line.split('  ',1)
            assert hashlib.sha256(archive.read(name)).hexdigest()==digest
        assert json.loads(archive.read('replay/environment.json'))['verified_at_export'] is False
        archive.extractall(tmp_path)
    # -S excludes site packages: exported replay must work without the API's
    # installed dependencies, and without source files from this checkout.
    script = '''import sys,shutil
sys.path.insert(0,sys.argv[1])
from engine import support
from engine.sandbox import Sandbox
from engine.parallel import execution_budget
support.configure('offline-test-image')
sandbox=Sandbox()
try:
 assert sandbox.settings.execution_concurrency==1
 with execution_budget(sandbox.settings.execution_concurrency,sandbox.check_cancelled):
  sandbox.check_cancelled()
finally:
 sandbox.close()
 shutil.rmtree(support.get_settings().work_dir)
'''
    run=subprocess.run([sys.executable,'-I','-S','-c',script,str(tmp_path/'replay')],
                       cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert run.returncode==0,run.stdout+run.stderr


@pytest.mark.sandbox
@pytest.mark.skipif(os.environ.get('RUN_SANDBOX_TESTS')!='1',reason='Real runtime required')
def test_real_linux_export_runs_offline_cli_with_frozen_judging_engine(owner,problem,tmp_path):
    with SessionLocal() as db:
        row=db.get(Problem,problem)
        row.document=populate(db,row)
        db.get(Revision,(problem,0)).document=copy.deepcopy(row.document)
        db.commit()
    state=owner.get(f'/api/problems/{problem}').json()
    response=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'package','version':state['version'],'options':{'export_format':'linux','execution_backend':'editor'}})
    assert response.status_code==202,response.text
    process_job(claim_job())
    job=owner.get(f'/api/problems/{problem}/jobs/{response.json()["id"]}').json()
    assert job['status']=='completed',job
    with zipfile.ZipFile(get_settings().data_dir/'packages'/(job['id']+'.zip')) as archive:
        archive.extractall(tmp_path)
    image=os.environ.get('EDITOR_RUNNER_IMAGE','zoj-editor-runtime:v1')
    run=subprocess.run([sys.executable,str(tmp_path/'replay/replay.py'),'--image',image],cwd=tmp_path,capture_output=True,text=True,timeout=180)
    assert run.returncode==0,run.stdout+run.stderr
    result=json.loads((tmp_path/'replay-result.json').read_text())
    assert result['verified'] and all(solution['matched'] for solution in result['solutions'])
    assert [row['verdict'] for row in result['solutions'][0]['cases']]==['AC']*3
    assert result['environment']['measured_environment']['python']
