import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from app.parallel import ordered_work, execution_budget, check_stopped
from app.sandbox import SandboxError
from test_invocation_scope import scoped_pipeline
from test_stress_invocations import stress_pipeline


class Activity:
    def __init__(self):
        self.lock=threading.Lock();self.active=0;self.peak=0
    def run(self, function, *args, **kwargs):
        with self.lock:
            self.active+=1;self.peak=max(self.peak,self.active)
        try:
            time.sleep(.02)
            return function(*args,**kwargs)
        finally:
            with self.lock:self.active-=1


def test_ordered_results_bound_prefetch_and_cancel_before_cleanup():
    started=[];lock=threading.Lock();ready=threading.Event();done=[]
    def work(index):
        with lock:
            started.append(index)
            if len(started)==3:ready.set()
        try:
            assert ready.wait(2)
            if index==0:return index
            while True:
                check_stopped();time.sleep(.005)
        finally:
            done.append(index)
    with ordered_work(work,range(1000),3) as results:
        assert next(results)==0
    assert set(started)==set(done)=={0,1,2}
    assert all(not t.name.startswith('editor-test') for t in threading.enumerate())


def test_failures_are_ordered_and_do_not_execute_the_entire_input():
    scheduled=[]
    def items():
        for i in range(100):scheduled.append(i);yield i
    def work(i):
        if i==1:raise SandboxError('case 1 failed')
        return i
    with pytest.raises(SandboxError,match='case 1 failed'):
        with ordered_work(work,items(),3) as results:
            assert next(results)==0
            next(results)
    assert len(scheduled)<=4


def test_shared_execution_limit_spans_independent_jobs_and_releases_on_error():
    activity=Activity()
    def work(i):
        with execution_budget(2,lambda:None):
            return activity.run(lambda:i)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(work,range(12)))==list(range(12))
    assert activity.peak==2 and activity.active==0
    with pytest.raises(ValueError):
        with execution_budget(2,lambda:None):raise ValueError('release')
    with execution_budget(2,lambda:None):pass


def test_preparation_parallelizes_tests_but_keeps_selection_and_order(problem):
    p,_=scoped_pipeline(problem,prepare_mode='inputs',test_indices=[2,3,4,5])
    p.parallelism=3
    tests=p.doc['testsets']['tests']['tests'];template=tests['1']
    for i in range(2,8):tests[str(i)]={**template,'index':i,'command':f'gen {i}'}
    before=copy.deepcopy(p.doc);activity=Activity()
    p.generator=lambda command:activity.run(lambda:(command+'\n').encode())
    p.prepare()
    assert [r['index'] for r in p.result['testsets']['tests']]==[2,3,4,5]
    assert activity.peak==3 and activity.active==0 and p.doc==before


def test_duplicate_preparation_still_fails_without_publishing_a_partial_set(problem):
    p,_=scoped_pipeline(problem,prepare_mode='inputs')
    p.parallelism=3;p.doc['info']['skip_duplicate_validation']=False
    tests=p.doc['testsets']['tests']['tests'];tests['2']={**tests['1'],'index':2}
    with pytest.raises(SandboxError,match='중복된 입력'):p.prepare()
    assert p.result['testsets']=={}


def parallel_stress(monkeypatch,**options):
    real=time.monotonic
    p,calls,_=stress_pipeline(monkeypatch,**options)
    monkeypatch.setattr(time,'monotonic',real)
    p.parallelism=3
    return p,calls


def test_stress_seeds_really_overlap_but_evidence_retains_seed_order(monkeypatch):
    p,calls=parallel_stress(monkeypatch,candidates=['wrong','slow'],iterations=6,counterexample_limit=10)
    activity=Activity();generate=p.generator
    p.generator=lambda command:activity.run(generate,command)
    p.stress()
    assert activity.peak==3 and activity.active==0
    assert [c['seed'] for c in p.result['counterexamples']]==list(range(1,7))
    assert all([f['verdict'] for f in c['findings']]==['WA','TLE'] for c in p.result['counterexamples'])
    assert p.result['stress']['parallelism']==3 and p.result['iterations']==6
    assert len(calls['main'])==6


def test_parallel_stress_deduplicates_inflight_input_and_keeps_first_seed(monkeypatch):
    p,calls=parallel_stress(monkeypatch,iterations=10,counterexample_limit=10)
    p.generator=lambda command:b'identical\n'
    generate=p.validator;activity=Activity()
    p.validator=lambda *args:activity.run(generate,*args)
    p.stress()
    assert len(calls['validator'])==len(calls['main'])==len(calls['wrong'])==1
    assert p.result['stress']['duplicates_skipped']==9
    assert [c['seed'] for c in p.result['counterexamples']]==[1]


def test_parallel_early_stop_keeps_lowest_failing_seed_and_bounds_lookahead(monkeypatch):
    p,calls=parallel_stress(monkeypatch,iterations=1000,stop_on_first=True)
    run=p.run_solution
    def slow_first(name,content,*args,**kwargs):
        if content==b'gen 1\n':time.sleep(.025)
        return run(name,content,*args,**kwargs)
    p.run_solution=slow_first;p.stress()
    assert p.result['iterations']==1 and p.result['counterexamples'][0]['seed']==1
    assert p.result['stress']['stop_reason']=='first_counterexample'
    assert len(calls['generator'])<=3


def test_parallel_focus_mode_still_covers_all_solutions_and_preserves_verdict_filter(monkeypatch):
    p,_=parallel_stress(monkeypatch,candidates=['wrong','slow'],iterations=8,stop_mode='all_candidates',counterexample_limit=1)
    original=p.run_solution
    def run(name,content,*args):
        if name=='slow' and content!=b'gen 3\n':return 'AC',{'output':b'42','stderr':'','cpu_ms':1,'memory_kb':1}
        return original(name,content,*args)
    p.run_solution=run;p.stress()
    assert p.result['stress']['stop_reason']=='all_candidates_covered'
    assert [c['seed'] for c in p.result['counterexamples']]==[1,3]
    assert p.result['uncovered_candidates']==[]


def test_parallel_stress_error_keeps_prior_evidence_and_does_not_claim_completion(monkeypatch):
    p,_=parallel_stress(monkeypatch,iterations=8)
    original=p.generator
    def generate(command):
        if command=='gen 2':raise SandboxError('bad generator')
        return original(command)
    p.generator=generate
    with pytest.raises(SandboxError,match='bad generator'):p.stress()
    assert p.result['iterations']==1 and p.result['stress']['last_seed']==2
    assert [c['seed'] for c in p.result['counterexamples']]==[1]


def test_parallel_stress_deadline_cancels_unfinished_work_without_claiming_tle(monkeypatch):
    p,_=parallel_stress(monkeypatch,iterations=100,duration_seconds=.03)
    original=p.generator
    def generate(command):
        time.sleep(.04);p.sandbox.check_cancelled();return original(command)
    p.generator=generate;p.stress()
    assert p.result['stress']['stop_reason']=='duration'
    assert p.result['iterations']==0 and p.result['counterexamples']==[]


def test_parallel_evidence_budget_keeps_verified_prefix(monkeypatch):
    monkeypatch.setattr('app.stress_search.MAX_STRESS_EVIDENCE_BYTES',1)
    p,_=parallel_stress(monkeypatch)
    p.stress()
    assert p.result['stress']['stop_reason']=='evidence_limit' and p.result['counterexamples']==[]


def test_parallel_blob_producers_share_one_metadata_row_and_enforce_total_quota(problem,monkeypatch):
    from app.db import SessionLocal,Blob
    from app.storage import put_blob
    from app.config import get_settings
    from sqlalchemy import select
    from fastapi import HTTPException
    barrier=threading.Barrier(4)
    def store(i):
        barrier.wait(timeout=2)
        with SessionLocal() as db:
            sha=put_blob(db,problem,b'parallel generated data')['sha256'];db.commit();return sha
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(store,range(4)))
    assert len(set(results))==1
    with SessionLocal() as db:assert len(db.scalars(select(Blob).where(Blob.problem_id==problem)).all())==1
    monkeypatch.setattr(get_settings(),'max_project_bytes',len(b'parallel generated data'))
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as error:put_blob(db,problem,b'extra')
        assert error.value.status_code==413
