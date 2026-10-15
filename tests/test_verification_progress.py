import base64
import copy
import hashlib
import threading
from types import SimpleNamespace

import pytest

from app import judge_bridge as bridge
from app.db import SessionLocal
from app.storage import put_blob, read_blob
from app.verification_progress import VerificationProgress
from test_invocation_scope import scoped_pipeline
from test_judge_bridge import fixture_data, record


def test_fast_later_solution_is_published_while_the_first_solution_is_blocked(problem):
    pipeline, _ = scoped_pipeline(problem, 'verify', solutions=['slow.cpp', 'fast.cpp'], test_indices=[])
    pipeline.doc['solutions'] = {'slow.cpp': {'tag': 'WA'}, 'fast.cpp': {'tag': 'OK'}}
    pipeline.remote_judging, pipeline.parallelism = False, 2
    snapshots, errors = [], []
    release, ready = threading.Event(), threading.Event()
    def store(content):
        with SessionLocal() as db:
            sha = put_blob(db, problem, content)['sha256']
            db.commit()
            return sha
    test = pipeline.doc['testsets']['tests']['tests']['1']
    test['answer_sha'] = store(b'42\n')
    pipeline.prepare = lambda: pipeline.result.update(testsets={'tests': [copy.deepcopy(test)]})
    pipeline.blob = store
    pipeline.checker = lambda *args: ('AC', {'stderr': ''})
    def run(name, *args):
        if name == 'slow.cpp':
            assert release.wait(3)
        return ('WA' if name == 'slow.cpp' else 'AC'), {'output': b'42\n', 'cpu_ms': 1, 'memory_kb': 1, 'wall_ms': 1, 'stderr': ''}
    pipeline.run_solution = run
    def publish(value):
        snapshots.append(copy.deepcopy(value))
        if any(row['name'] == 'fast.cpp' and row['state'] == 'completed' for row in value['solutions']):
            ready.set()
    pipeline.publish = publish
    def execute():
        try: pipeline.verify()
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert ready.wait(2), 'Later solution was hidden behind the first result'
        assert len(snapshots[-1]['solutions']) == 2
        assert snapshots[-1]['verification_progress']['current'] == 1
        assert snapshots[-1]['verification_progress']['percent'] == 50
        assert snapshots[-1]['solutions'][0]['cases'] == []
        assert snapshots[-1]['solutions'][1]['cases'][0]['verdict'] == 'AC'
    finally:
        release.set()
        thread.join(3)
    assert not errors and not thread.is_alive()
    assert pipeline.result['verification_progress']['percent'] == 100
    assert pipeline.result['verified']
    assert [row['name'] for row in pipeline.result['solutions']] == ['slow.cpp', 'fast.cpp']


def test_progress_counts_requested_pairs_and_does_not_turn_a_compile_error_into_test_completion(problem):
    pipeline, _ = scoped_pipeline(problem, 'verify', test_indices=[1])
    pipeline.doc['solutions'] = {'main.cpp': {'tag': 'MA'}, 'wrong.cpp': {'tag': 'WA'}, 'skip.cpp': {'tag': 'NR'}}
    pipeline.publish = lambda *_: None
    tracker = VerificationProgress(pipeline, list(pipeline.doc['solutions']))
    assert pipeline.result['verification_progress']['total'] == 2
    tracker.begin()
    tracker.case('wrong.cpp', {'testset': 'tests', 'index': 1, 'verdict': 'WA'})
    tracker.finish_solution({'name': 'main.cpp', 'cases': [{'testset': 'tests', 'index': 0, 'verdict': 'CE'}], 'matched': False})
    tracker.finish()
    assert pipeline.result['verification_progress']['percent'] == 50
    assert tracker.rows['skip.cpp']['total_tests'] == 0


def test_remote_partial_results_and_binary_outputs_are_visible_before_the_run_finishes(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path, 'WA')
    snapshots = []
    pipeline.publish = lambda result: snapshots.append(copy.deepcopy(result))
    pipeline.verification = VerificationProgress(pipeline, ['main.cpp'])
    pipeline.verification.begin()
    def store(content):
        with SessionLocal() as db:
            sha = put_blob(db, problem, content)['sha256']
            db.commit()
            return sha
    pipeline.blob = store
    content = b'\x00\xff\r\n'
    sha = hashlib.sha256(content).hexdigest()
    final = record('wrong_answer', 1)
    final['result']['testcase_results'][0].update(output_available=True, output_size=4, output_total_bytes=10, output_truncated=True, output_sha256=sha)
    partial = record('judging')
    partial['result']['testcase_results'] = [final['result']['testcase_results'][0]]
    calls = []
    states = iter([partial, final])
    def remote(method, path, *args, **kwargs):
        calls.append((method, path))
        if path.endswith('/output'):
            return {'base64': base64.b64encode(content).decode()}
        if method == 'POST': return record('judging')
        return next(states)
    monkeypatch.setattr(bridge, 'remote', remote)
    monkeypatch.setattr(bridge.time, 'sleep', lambda *_: None)
    result = bridge.verify_solution(pipeline, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())
    assert result['complete'] and result['matched']
    assert any(snapshot['verification_progress']['current'] == 1 and snapshot['solutions'][0]['cases'][0]['actual_sha'] == sha for snapshot in snapshots)
    assert read_blob(sha) == content
    assert result['cases'][0]['output_truncated'] and result['cases'][0]['output_total_bytes'] == 10
    assert len([call for call in calls if call[0] == 'POST']) == 1
    assert len([call for call in calls if call[1].endswith('/output')]) == 1
    assert all('/remaining' not in path for _, path in calls)


def test_legacy_aggregate_without_case_results_cannot_fabricate_full_acceptance(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path)
    response = record()
    response['result'].pop('testcase_results')
    response['result'].pop('full_suite_complete')
    monkeypatch.setattr(bridge, 'remote', lambda *args, **kwargs: response)
    with pytest.raises(bridge.SandboxError, match='전체 테스트의 개별 결과'):
        bridge.verify_solution(pipeline, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())


def test_one_incomplete_set_keeps_later_sets_results_without_claiming_completion(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path)
    pipeline.job.kind = 'package'
    pipeline.doc['testsets']['later'] = copy.deepcopy(pipeline.doc['testsets']['tests'])
    pipeline.result['testsets']['later'] = copy.deepcopy(pipeline.result['testsets']['tests'])
    failure = record('system_error')
    failure['result'].update(full_suite_complete=False, testcase_results=[])
    replies = iter([failure, record()])
    monkeypatch.setattr(bridge, 'remote', lambda *args, **kwargs: next(replies))
    result = bridge.verify_solution(pipeline, SimpleNamespace(email='author@example.com'), 'main.cpp', threading.Event())
    assert not result['complete'] and not result['matched']
    assert len(result['judge_runs']) == 2
    assert any(case['testset'] == 'tests' and case['index'] == 0 and case['verdict'] == 'JUDGE_ERROR' for case in result['cases'])
    assert [case['verdict'] for case in result['cases'] if case['testset'] == 'later'] == ['AC'] * 3
