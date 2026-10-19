import base64
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
import sys
from uuid import uuid4
import pytest
from fastapi import HTTPException
from app import agent_transport, judge_bridge
from app.config import get_settings
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.execution_protocol import unpack
from app.sandbox import Sandbox, SandboxError
from app.worker import claim_job, process_job
from test_judge_requests import clock


@pytest.fixture
def agent_mode(monkeypatch):
    monkeypatch.setattr(get_settings(),'execution_transport','agent')
    caps={'backend':'zoj','runtime_revision':'test-judge-v1','execution':{'protocol':'editor-execute-v1','online_nodes':1,'runtime_revision':'test-executor-v1'}}
    monkeypatch.setattr(judge_bridge,'remote',lambda *args,**kwargs:copy.deepcopy(caps))
    return caps


def test_remote_transport_restores_outputs_and_never_runs_local_docker(agent_mode,monkeypatch):
    calls=[]
    def remote(method,path,user,package=None,sha=None,**kw):
        calls.append((method,path))
        if method=='POST':
            content=package.read_bytes();assert hashlib.sha256(content).hexdigest()==sha
            value=unpack(content);assert value['files']['.input']['base64']==base64.b64encode(b'1 2\n').decode()
            return {'id':'task','status':'waiting'}
        return {'id':'task','status':'completed','node_name':'judge-1','agent_version':'0.2.22','result':{'status':'ok','exit_code':0,'cpu_ms':17,'wall_ms':21,'memory_kb':5000,'stderr':'','output_base64':'Mwo=','artifacts':{}}}
    monkeypatch.setattr(agent_transport,'remote',remote)
    monkeypatch.setattr('app.sandbox.subprocess.run',lambda *a,**kw:pytest.fail('Local Docker must never execute production tools'))
    sandbox=Sandbox(remote_context={'email':'author@example.com','job_kind':'prepare','runtime_revision':'test-runtime'})
    try:
        folder=sandbox.new_folder();(folder/'sum.py').write_text('print(sum(map(int,input().split())))')
        result=sandbox.execute(folder,['python3','sum.py'],b'1 2\n')
        assert result['output']==b'3\n' and sandbox.agent_transport.records[0]['node_name']=='judge-1'
        assert calls==[('POST','/api/editor/judge/tasks'),('GET','/api/editor/judge/tasks/task?wait_seconds=2')]
    finally:sandbox.close()


def test_transport_waits_for_upload_and_retries_busy_status(agent_mode, monkeypatch, clock):
    calls = []
    def remote(method, path, *args, **kwargs):
        calls.append(method)
        if method == 'POST':
            return {'id': 'task', 'status': 'uploading'}
        if calls.count('GET') == 1:
            raise HTTPException(429)
        return {'id': 'task', 'status': 'completed', 'result': {'status': 'ok', 'exit_code': 0,
                'cpu_ms': 17, 'wall_ms': 21, 'memory_kb': 5000, 'stderr': '', 'output_base64': 'Mwo=', 'artifacts': {}}}
    monkeypatch.setattr(agent_transport, 'remote', remote)
    sandbox = Sandbox(remote_context={'email': 'author@example.com', 'job_kind': 'prepare', 'runtime_revision': 'runtime'})
    try:
        folder = sandbox.new_folder()
        (folder / 'sum.py').write_text('print(3)')
        assert sandbox.execute(folder, ['python3', 'sum.py'])['output'] == b'3\n'
        assert calls == ['POST', 'GET', 'GET']
    finally:
        sandbox.close()


def test_job_admission_requires_ready_nodes_and_preserves_the_transport_in_cache(owner,problem,agent_mode):
    agent_mode['execution']['online_nodes']=0
    created=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'render','version':0})
    assert created.status_code==503
    agent_mode['execution']['online_nodes']=1
    created=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'render','version':0})
    assert created.status_code==202,created.text
    options=created.json()['options']
    assert options['execution_transport']=='agent' and options['execution_revision']=='test-executor-v1'


def test_cancelled_transport_cancels_the_queued_agent_task(agent_mode,monkeypatch):
    cancelled=False; calls=[]
    def remote(method,path,*args,**kw):
        nonlocal cancelled
        calls.append(method)
        if method=='POST':cancelled=True;return {'id':'task','status':'waiting'}
        return {}
    monkeypatch.setattr(agent_transport,'remote',remote)
    def check():
        if cancelled:raise RuntimeError('cancelled')
    sandbox=Sandbox(check,{'email':'author@example.com','job_kind':'render','runtime_revision':'test-runtime'})
    try:
        folder=sandbox.new_folder();(folder/'x.py').write_text('print(1)')
        with pytest.raises(RuntimeError,match='cancelled'):sandbox.execute(folder,['python3','x.py'])
        assert calls==['POST','DELETE']
    finally:sandbox.close()


@pytest.fixture
def native_agent(agent_mode,monkeypatch,tmp_path):
    source=os.environ.get('JUDGE_AGENT_SOURCE_DIR')
    if os.environ.get('RUN_AGENT_WORKFLOW_TESTS')!='1' or not source:
        pytest.skip('Optional cross-repository actual agent workflow check')
    spec=importlib.util.spec_from_file_location('zoj_agent_test_runtime',Path(source)/'app/__init__.py',submodule_search_locations=[str(Path(source)/'app')])
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    from zoj_agent_test_runtime import editor_executor as native
    monkeypatch.setattr(native,'settings',SimpleNamespace(work_root=tmp_path/'agent',internal_api_base_url='http://localhost/api',object_read_timeout_seconds=10,
                    editor_runtime_image=os.environ['JUDGE_EDITOR_RUNTIME_IMAGE'],editor_renderer_image=os.environ['JUDGE_EDITOR_RENDERER_IMAGE']))
    executor=native.EditorExecutor();assert executor.capabilities
    tasks={}
    def remote(method,path,user,package=None,sha=None,**kw):
        if method=='POST':
            value=unpack(package.read_bytes());task_id=uuid4().hex
            result=executor.execute({'input_url':package.resolve().as_uri(),'input_sha256':sha,'runtime':value['runtime']})
            tasks[task_id]={'id':task_id,'status':'completed','node_name':'actual-agent-runtime','agent_version':'0.2.22','execution_revision':executor.revision,'result':result}
            return {'id':task_id,'status':'waiting'}
        if method=='GET':return copy.deepcopy(tasks[path.rsplit('/',1)[1].split('?')[0]])
        return {'status':'cancelled'}
    monkeypatch.setattr(agent_transport,'remote',remote)
    return tasks


@pytest.mark.sandbox
@pytest.mark.parametrize('kind',['prepare','verify','validator-tests','checker-tests','stress','package','render'])
def test_legacy_editor_workflows_execute_on_actual_agent_runtime(owner,problem,native_agent,kind,queue_legacy_job):
    with SessionLocal() as db:
        row=db.get(Problem,problem);row.document=populate(db,row)
        if kind == 'checker-tests':
            from app.storage import put_blob
            inp = put_blob(db,problem,b'1 2\n')['sha256']
            ans = put_blob(db,problem,b'3\n')['sha256']
            wrong = put_blob(db,problem,b'0\n')['sha256']
            row.document = {**row.document, 'checker_tests': {'1':{'input_sha':inp,'output_sha':ans,'answer_sha':ans,'expected':'OK'},
                                            '2':{'input_sha':inp,'output_sha':wrong,'answer_sha':ans,'expected':'WRONG_ANSWER'}}}
        db.get(Revision,(problem,0)).document=row.document;db.commit()
    state=owner.get(f'/api/problems/{problem}').json()
    options={'execution_backend':'editor'}
    if kind=='prepare':options['auto_apply']=True
    if kind=='stress':options.update(generator='gen {seed}',reference='main.cpp',candidate='wrong.py',iterations=2)
    created=queue_legacy_job(owner,problem,{'kind':kind,'version':state['version'],'options':options})
    assert created.status_code==202,created.text
    process_job(claim_job())
    job=owner.get(f'/api/problems/{problem}/jobs/{created.json()["id"]}').json()
    assert job['status']=='completed',job
    assert job['result']['runtime']['backend']=='agent'
    assert native_agent and job['result']['execution_runs']
    assert all(run['node_name']=='actual-agent-runtime' for run in job['result']['execution_runs'])
    if kind=='prepare':assert job['application'] and job['result']['auto_apply']['status']=='applied'
    if kind in {'verify','package'}:
        assert job['result']['verified']
        assert any(s['name']=='wrong.py' and any(c['verdict']=='WA' for c in s['cases']) for s in job['result']['solutions'])
    if kind=='stress':assert job['result']['counterexample']['verdict']=='WA'
    if kind=='render':assert job['result']['statements']['korean']['pdf']=='OK'
    if kind=='package':assert owner.get(f'/api/problems/{problem}/packages/{job["id"]}').content.startswith(b'PK')



def test_lost_creation_acknowledgement_is_cancelled_by_operation(agent_mode,monkeypatch,clock):
    from fastapi import HTTPException
    cancelled=False;calls=[]
    def remote(method,path,*args,**kw):
        nonlocal cancelled
        calls.append((method,path))
        if method=='POST':cancelled=True;raise HTTPException(502,{'message':'lost acknowledgement'})
        return {}
    monkeypatch.setattr(agent_transport,'remote',remote)
    def check():
        if cancelled:raise RuntimeError('cancelled')
    sandbox=Sandbox(check,{'email':'author@example.com','job_kind':'render','runtime_revision':'runtime'})
    try:
        folder=sandbox.new_folder();(folder/'x.py').write_text('print(1)')
        with pytest.raises(RuntimeError,match='cancelled'):sandbox.execute(folder,['python3','x.py'])
        assert calls[1][0]=='DELETE' and calls[1][1].startswith('/api/editor/judge/task-operations/')
    finally:sandbox.close()
