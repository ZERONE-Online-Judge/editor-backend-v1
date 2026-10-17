import hashlib
import json
import threading
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app import judge_bridge as bridge
from test_judge_requests import clock


def fixture_data(problem, tmp_path, tag="MA"):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc['solutions']['main.cpp']['tag'] = tag
        tests = list(doc['testsets']['tests']['tests'].values())
        job = SimpleNamespace(id="a" * 32, problem_id=problem, document_hash="b" * 64,
                              kind="verify", options={"solutions": ['main.cpp'], "testset": "tests", "test_indices": []}, user_id=row.owner_id)
    return SimpleNamespace(doc=doc, job=job, result={"testsets": {"tests": tests}},
        sandbox=SimpleNamespace(root=tmp_path, check_cancelled=lambda: None),
        event=lambda *args, **kwargs: None,
        expected_matches=__import__('app.pipeline', fromlist=['Pipeline']).Pipeline.expected_matches)


def record(status="accepted", failure=None, run_id="run", statuses=None):
    outcomes = statuses or ['accepted' if index != failure else status for index in [1, 2, 3]]
    return {"id": run_id, "status": status, "progress_current": 3, "progress_total": 3,
            "node_name": "judge-agent-3", "agent_version": "0.2.21", "runtime_revision": "real-judge-v1",
            "result": {"final_status": status, "compile_message": None, "judge_message": None,
                       "failed_testcase_order": failure, "runtime_ms": 41, "memory_kb": 2048,
                       "full_suite_complete": status not in bridge.ACTIVE,
                       "testcase_results": [] if status in bridge.ACTIVE else [
                           {"index": index, "final_status": verdict, "runtime_ms": index, "memory_kb": index * 1024, "judge_message": ""}
                           for index, verdict in enumerate(outcomes, 1)]}}


def test_snapshot_is_immutable_deterministic_and_has_verified_hashes(problem, tmp_path):
    p = fixture_data(problem, tmp_path)
    tests = p.result['testsets']['tests']
    path, digest = bridge.archive(p, 'main.cpp', 'tests', tests, 0)
    content = path.read_bytes()
    assert hashlib.sha256(content).hexdigest() == digest
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['source']['language'] == 'cpp17'
        assert manifest['collect_all_results'] is True
        assert manifest['project_id'] == problem
        assert len(manifest['testcases']) == 3
        assert sum(r['role'] == 'checker' for r in manifest['resources']) == 1
        for name in z.namelist():
            if name.startswith('blobs/'):
                assert hashlib.sha256(z.read(name)).hexdigest() == name.split('/')[1]
    path2, digest2 = bridge.archive(p, 'main.cpp', 'tests', tests, 0)
    assert digest2 == digest and path2.read_bytes() == content


def test_full_acceptance_records_real_node_and_does_not_fabricate_case_time(problem, tmp_path, monkeypatch):
    p = fixture_data(problem, tmp_path)
    calls = []
    def remote(method, path, *args, **kwargs):
        calls.append((method, path))
        return record()
    monkeypatch.setattr(bridge, 'remote', remote)
    result = bridge.verify_solution(p, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())
    assert result['matched'] and result['complete']
    assert len(result['cases']) == 3 and all(c['verdict'] == 'AC' for c in result['cases'])
    assert [c['cpu_ms'] for c in result['cases']] == [1, 2, 3]
    assert all(c['actual_sha'] is None for c in result['cases'])
    assert result['judge_runs'][0]['runtime_ms'] == 41
    assert result['judge_runs'][0]['node_name'] == 'judge-agent-3'
    assert len(calls) == 1


def test_wrong_answer_still_returns_the_full_suite_in_one_request(problem, tmp_path, monkeypatch):
    p = fixture_data(problem, tmp_path, tag='WA')
    first_index = p.result['testsets']['tests'][0]['index']
    calls = []
    def remote(method, path, user, package=None, digest=None, **kwargs):
        calls.append((method, path, package is not None))
        return record('wrong_answer', first_index, 'first')
    monkeypatch.setattr(bridge, 'remote', remote)
    result = bridge.verify_solution(p, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())
    assert result['matched'] and result['complete']
    assert len(result['cases']) == 3
    assert [c['verdict'] for c in result['cases']].count('WA') == 1
    assert calls == [('POST', '/api/editor/judge/runs', True)]
    assert len(result['judge_runs']) == 1


def test_to_tag_detects_wrong_answer_after_timeout(problem, tmp_path, monkeypatch):
    p = fixture_data(problem, tmp_path, tag='TO')
    indices = [t['index'] for t in p.result['testsets']['tests']]
    monkeypatch.setattr(bridge, 'remote', lambda *a, **kw: record('time_limit_exceeded', indices[0], 'timeout', ['time_limit_exceeded', 'wrong_answer', 'accepted']))
    result = bridge.verify_solution(p, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())
    assert result['complete'] and not result['matched']
    assert {c['verdict'] for c in result['cases']} == {'AC', 'TLE', 'WA'}


def test_cancellation_stops_remote_work(problem, tmp_path, monkeypatch):
    p = fixture_data(problem, tmp_path)
    calls = []
    def remote(method, path, *a, **kw):
        calls.append(method)
        value = record('judging')
        if method == 'POST':
            p.sandbox.check_cancelled = lambda: (_ for _ in ()).throw(RuntimeError('cancelled'))
        return value
    monkeypatch.setattr(bridge, 'remote', remote)
    with pytest.raises(RuntimeError, match='cancelled'):
        bridge.run_batch(p, SimpleNamespace(email='author@example.com'), 'main.cpp', 'tests', p.result['testsets']['tests'], 0, threading.Event())
    assert calls == ['POST', 'DELETE']
    assert not list(tmp_path.glob('*.judge.zip'))


def test_registration_and_polling_survive_busy_api(problem, tmp_path, monkeypatch, clock):
    p = fixture_data(problem, tmp_path)
    calls, snapshots = [], []
    def remote(method, path, *args, **kwargs):
        calls.append(method)
        if method == 'POST':
            snapshots.append(args[1].read_bytes())
            if len(snapshots) <= 4:
                raise HTTPException(503)
            return record('uploading')
        if calls.count('GET') <= 2:
            raise HTTPException(429 if calls.count('GET') == 1 else 503)
        return record()
    monkeypatch.setattr(bridge, 'remote', remote)
    result = bridge.verify_solution(p, object(), 'main.cpp', threading.Event())
    assert result['complete'] and result['matched'] and len(result['cases']) == 3
    assert len(snapshots) == 5 and all(value == snapshots[0] for value in snapshots)
    assert calls.count('GET') == 3 and 'DELETE' not in calls


def test_lost_registration_response_cancels_by_operation(problem, tmp_path, monkeypatch, clock):
    p = fixture_data(problem, tmp_path)
    calls = []
    def remote(method, path, *args, **kwargs):
        calls.append((method, path))
        if method == 'POST':
            raise HTTPException(503)
        return {}
    monkeypatch.setattr(bridge, 'remote', remote)
    with pytest.raises(HTTPException):
        bridge.run_batch(p, object(), 'main.cpp', 'tests', p.result['testsets']['tests'], 0, threading.Event())
    assert calls[-1][0] == 'DELETE' and '/run-operations/' in calls[-1][1]
    assert not list(tmp_path.glob('*.judge.zip'))


def test_job_backend_and_real_judge_revision_invalidate_cached_results(owner, problem, monkeypatch):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        db.get(Revision, (problem, 0)).document = row.document
        db.commit()
    revision = ['judge-v1']
    monkeypatch.setattr(bridge, 'capabilities', lambda user: {'runtime_revision': revision[0]})
    state = owner.get(f'/api/problems/{problem}').json()
    request = {'kind': 'verify', 'version': state['version'], 'options': {'execution_backend': 'zoj'}}
    a = owner.post(f'/api/problems/{problem}/jobs', json=request)
    assert a.status_code == 202, a.text
    assert a.json()['options']['execution_backend'] == 'zoj'
    assert owner.post(f'/api/problems/{problem}/jobs', json=request).json()['id'] == a.json()['id']
    revision[0] = 'judge-v2'
    b = owner.post(f'/api/problems/{problem}/jobs', json=request)
    assert b.status_code == 202 and b.json()['id'] != a.json()['id']
    request['options']['execution_backend'] = 'editor'
    c = owner.post(f'/api/problems/{problem}/jobs', json=request)
    assert c.status_code == 202 and c.json()['id'] not in {a.json()['id'], b.json()['id']}


def test_unsupported_features_require_explicit_editor_backend(problem, tmp_path):
    p = fixture_data(problem, tmp_path)
    p.doc['solutions']['main.cpp']['language'] = 'cpp20'
    with pytest.raises(HTTPException) as error:
        bridge.compatible(p.doc, p.job.options, 'verify')
    assert error.value.detail['code'] == 'judge_incompatible'
    assert '자동으로 바꾸지 않습니다' in error.value.detail['guidance']
    assert error.value.detail['unsupported_solutions']==[{'name':'main.cpp','language':'cpp20','label':'C++20'}]


def test_group_expected_tags_are_supported_without_partial_score_semantic_changes(problem,tmp_path,monkeypatch):
    p=fixture_data(problem,tmp_path,tag='TL')
    p.doc['testsets']['tests']['groups_enabled']=True
    p.doc['testsets']['tests']['groups']={name:{'points_policy':'COMPLETE_GROUP','feedback_policy':'COMPLETE','dependencies':[]} for name in ['small','large']}
    for test in p.result['testsets']['tests']:
        test['group']='large' if test['index']==3 else 'small'
    p.doc['solutions']['main.cpp']['group_tags']={'tests':{'small':'OK','large':'TL'}}
    bridge.compatible(p.doc,p.job.options,'verify')
    monkeypatch.setattr(bridge,'remote',lambda *args,**kwargs: record('time_limit_exceeded', 3))
    result=bridge.verify_solution(p,object(),'main.cpp',threading.Event())
    assert result['matched'] and result['expectations_complete']
    assert [case['verdict'] for case in result['cases']]==['AC','AC','TLE']


def test_language_diagnostics_group_files_and_respect_selected_scope(problem,tmp_path):
    p=fixture_data(problem,tmp_path)
    for i in range(7):
        p.doc['solutions'][f'wrong-{i}.cpp']={**p.doc['solutions']['main.cpp'],'name':f'wrong-{i}.cpp','language':'cpp20','tag':'WA'}
    with pytest.raises(HTTPException) as error:bridge.compatible(p.doc,{**p.job.options,'solutions':[]},'verify')
    detail=error.value.detail
    assert len(detail['unsupported_solutions'])==7
    assert all(row['label']=='C++20' for row in detail['unsupported_solutions'])
    assert '7개' in detail['message'] and 'wrong-0' not in detail['message']
    assert len(detail['supported_languages'])==4 and detail['suggested_backend']=='editor'
    assert not detail['issues']
    bridge.compatible(p.doc,{**p.job.options,'solutions':['main.cpp']},'verify')
    assert all(s['language']=='cpp20' for name,s in p.doc['solutions'].items() if name.startswith('wrong-'))
