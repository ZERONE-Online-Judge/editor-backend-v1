import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import reference_generation as reference
from app.agent_sandbox import AgentSandbox, require_agent_job
from app.pipeline import Pipeline
from app.sandbox import SandboxError
from test_judge_bridge import fixture_data
from app.db import SessionLocal
from app.storage import put_blob


def test_online_execution_never_falls_back_to_editor_local_runtime(monkeypatch):
    for kind in ['prepare','verify','stress','package','render','validator-tests','checker-tests']:
        with pytest.raises(HTTPException):
            require_agent_job(SimpleNamespace(kind=kind, options={'execution_transport':'local','execution_backend':'editor'}))
    with pytest.raises(SandboxError):
        AgentSandbox()
    sandbox = AgentSandbox(remote_context={'email':'test@example.com','job_kind':'prepare','runtime_revision':'v1'})
    try:
        assert sandbox.agent_transport is not None
        sandbox.agent_transport = None
        monkeypatch.setattr('app.sandbox.subprocess.Popen', lambda *_a, **_k: pytest.fail('Local processes must never start'))
        with pytest.raises(SandboxError):
            sandbox._execute(None, None)
    finally:
        sandbox.close()


@pytest.mark.parametrize('verdict,truncated,missing', [('AC',False,False),('TLE',False,False),('AC',True,False),('AC',False,True)])
def test_reference_requires_full_output_and_preserves_native_verdict(problem,tmp_path,monkeypatch,verdict,truncated,missing):
    p = fixture_data(problem,tmp_path)
    p.blob = lambda content: save(p.job.problem_id, content)
    sha = p.blob(b'3\n')
    p.stress_judge_budget = threading.BoundedSemaphore(1)
    p.result_lock = threading.RLock()
    p.publish = lambda _: None
    def batch(proxy,identity,name,set_name,tests,attempt,cancel):
        assert proxy.reference_output and proxy.doc is p.doc and proxy.job is p.job
        assert tests[0]['input_sha'] == p.blob(b'1 2\n')
        return {'id':'run','status':'accepted' if verdict=='AC' else 'time_limit_exceeded',
                'node_name':'agent1','cases':[{'verdict':verdict,'actual_sha':None if missing else sha,
                'output_truncated':truncated,'cpu_ms':123,'memory_kb':234,'wall_ms':None,'comment':''}]}
    monkeypatch.setattr(reference,'run_batch',batch)
    if truncated or missing:
        with pytest.raises(SandboxError): reference.run(p,'main.cpp',b'1 2\n')
    else:
        status,result=reference.run(p,'main.cpp',b'1 2\n')
        assert status==verdict and result['cpu_ms']==123 and result['output']==b'3\n'
        assert p.result['execution_runs'][-1]['runtime']=='reference'
    assert p.stress_judge_budget.acquire(blocking=False)


def save(problem,content):
    with SessionLocal() as db:
        sha=put_blob(db,problem,content)['sha256']
        db.commit()
        return sha


def test_pipeline_main_generation_cannot_use_preparation_runner(monkeypatch):
    p=SimpleNamespace(native_reference=True)
    monkeypatch.setattr(reference,'run',lambda *args: ('AC',{'native':True}))
    assert Pipeline.run_solution(p,'main.py',b'1',preparation=True)==('AC',{'native':True})
