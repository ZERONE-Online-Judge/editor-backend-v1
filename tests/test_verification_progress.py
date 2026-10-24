import base64
import copy
import hashlib
import threading
from types import SimpleNamespace

import pytest

from app import judge_bridge as bridge
from app.db import SessionLocal
from app.storage import put_blob, read_blob
from app.sandbox import SandboxError
from app.verification_progress import VerificationProgress
from test_invocation_scope import scoped_pipeline
from test_judge_bridge import fixture_data, record


def complete_preparation(tracker):
    for (set_name, index), stages in tracker.preparation_plan.items():
        for stage in stages:
            tracker.preparation_step(set_name, index, stage)


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
    def prepare():
        pipeline.result.update(testsets={'tests': [copy.deepcopy(test)]})
        complete_preparation(pipeline.verification)
    pipeline.prepare = prepare
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
        assert snapshots[-1]['verification_progress']['judging']['current'] == 1
        assert snapshots[-1]['verification_progress']['percent'] == 85
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
    assert pipeline.result['verification_progress']['total'] == 7
    complete_preparation(tracker)
    tracker.begin()
    tracker.case('wrong.cpp', {'testset': 'tests', 'index': 1, 'verdict': 'WA'})
    tracker.finish_solution({'name': 'main.cpp', 'cases': [{'testset': 'tests', 'index': 0, 'verdict': 'CE'}], 'matched': False})
    tracker.finish()
    assert pipeline.result['verification_progress']['percent'] == 85
    assert pipeline.result['verification_progress']['judging'] == {'current': 1, 'total': 2, 'percent': 50}
    assert tracker.rows['skip.cpp']['total_tests'] == 0


def test_remote_partial_results_and_binary_outputs_are_visible_before_the_run_finishes(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path, 'WA')
    snapshots = []
    pipeline.publish = lambda result: snapshots.append(copy.deepcopy(result))
    pipeline.verification = VerificationProgress(pipeline, ['main.cpp'])
    complete_preparation(pipeline.verification)
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
    assert any(snapshot['verification_progress']['judging']['current'] == 1 and snapshot['solutions'][0]['cases'][0]['actual_sha'] == sha for snapshot in snapshots)
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


def test_preparation_reports_real_steps_before_any_judging_and_keeps_progress_at_transition(problem):
    p, _ = scoped_pipeline(problem, 'verify', test_indices=[1])
    p.doc['solutions'] = {'main.cpp': {'tag': 'MA'}}
    snapshots = []
    p.publish = lambda value: snapshots.append(copy.deepcopy(value))
    p.verification = tracker = VerificationProgress(p, ['main.cpp'])
    def validator(*args):
        progress = p.result['verification_progress']
        assert progress['phase'] == 'preparing'
        assert progress['current'] == 1 and progress['percent'] == 16
        assert progress['preparation']['stages']['input']['percent'] == 100
        assert progress['judging']['current'] == 0
        return True, {}
    p.validator = validator
    original = p.run_solution
    def run(*args, **kwargs):
        # The worker heartbeat must already see the validator result while
        # a long reference answer runs, even inside the publish throttle.
        published = snapshots[-1]['verification_progress']
        assert published['preparation']['stages']['validator']['current'] == 1
        assert published['current'] == 2
        return original(*args, **kwargs)
    p.run_solution = run
    p.prepare()
    progress = p.result['verification_progress']
    assert progress['preparation']['current'] == progress['preparation']['total'] == 5
    assert progress['current'] == 5 and progress['percent'] == 83
    tracker.begin()
    assert progress['phase'] == 'judging' and progress['percent'] == 83
    tracker.case('main.cpp', {'testset': 'tests', 'index': 1, 'verdict': 'AC'})
    tracker.finish()
    assert progress['percent'] == 100
    values = [s['verification_progress']['percent'] for s in snapshots]
    assert values == sorted(values) and values[0] == 0 and values[-1] == 100


@pytest.mark.parametrize('failure,completed', [('validator', 1), ('answer', 2), ('checker', 3), ('storage', 4), ('cancel', 1)])
def test_failed_or_cancelled_preparation_does_not_count_unfinished_work(problem, failure, completed):
    p, _ = scoped_pipeline(problem, 'verify', test_indices=[1])
    p.doc['solutions'] = {'main.cpp': {'tag': 'MA'}}
    p.publish = lambda *_: None
    p.verification = VerificationProgress(p, ['main.cpp'])
    if failure == 'validator':
        p.validator = lambda *args: (False, {'stderr': 'invalid input'})
    elif failure == 'answer':
        p.run_solution = lambda *args, **kwargs: ('TLE', {'stderr': ''})
    elif failure == 'checker':
        p.checker = lambda *args: ('WA', {'stderr': ''})
    elif failure == 'storage':
        p.blob = lambda *_: (_ for _ in ()).throw(SandboxError('storage failed'))
    else:
        p.validator = lambda *_: (_ for _ in ()).throw(SandboxError('cancelled'))
    with pytest.raises(SandboxError):
        p.prepare()
    progress = p.result['verification_progress']
    assert progress['phase'] == 'preparing'
    assert progress['preparation']['current'] == completed
    assert progress['preparation']['stages']['ready']['current'] == 0
    assert progress['judging']['current'] == 0
    assert progress['percent'] < 100


def test_duplicate_input_is_not_counted_as_ready(problem):
    p, _ = scoped_pipeline(problem, 'verify', test_indices=[])
    p.doc['solutions'] = {'main.cpp': {'tag': 'MA'}}
    p.doc['info']['skip_duplicate_validation'] = False
    tests = p.doc['testsets']['tests']['tests']
    tests['2'] = {**tests['1'], 'index': 2}
    p.publish = lambda *_: None
    p.verification = VerificationProgress(p, ['main.cpp'])
    with pytest.raises(SandboxError, match='중복된 입력'):
        p.prepare()
    preparation = p.result['verification_progress']['preparation']
    assert preparation['current'] == 9 and preparation['total'] == 10
    assert preparation['stages']['ready']['current'] == 1


def test_parallel_preparation_counts_each_selected_step_once(problem):
    from concurrent.futures import ThreadPoolExecutor
    p, _ = scoped_pipeline(problem, 'verify', test_indices=[2], all_testsets=True)
    p.doc['solutions'] = {'main.cpp': {'tag': 'MA'}, 'skip.cpp': {'tag': 'NR'}}
    for testset in p.doc['testsets'].values():
        testset['tests']['2'] = {**testset['tests']['1'], 'index': 2, 'sample': True,
                                'statement_input': 'example', 'verify_statement_io': True}
    p.publish = lambda *_: None
    tracker = VerificationProgress(p, list(p.doc['solutions']))
    planned = [(name, index, stage) for (name, index), stages in tracker.preparation_plan.items() for stage in stages]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda args: tracker.preparation_step(*args), planned * 5))
    tracker.preparation_step('tests', 1, 'input')
    tracker.preparation_step('unknown', 2, 'input')
    tracker.preparation_step('tests', 2, 'unknown')
    progress = p.result['verification_progress']
    assert progress['preparation']['current'] == progress['preparation']['total'] == 18
    assert progress['judging']['total'] == 3
    assert progress['preparation']['stages']['sample']['current'] == 3
    assert all(value['preparation']['current'] == value['preparation']['total'] == 6 for value in progress['testsets'].values())
    assert tracker.rows['skip.cpp']['total_tests'] == 0


def test_disabled_display_sample_check_is_not_planned_as_work(problem):
    p, _ = scoped_pipeline(problem, 'verify', test_indices=[1])
    p.doc['solutions'] = {'main.cpp': {'tag': 'MA'}}
    test = p.doc['testsets']['tests']['tests']['1']
    test.update(sample=True, statement_input='example', verify_statement_io=False)
    p.publish = lambda *_: None
    p.verification = VerificationProgress(p, ['main.cpp'])
    p.prepare()
    progress = p.result['verification_progress']
    assert progress['preparation']['current'] == progress['preparation']['total'] == 5
    assert progress['preparation']['stages']['sample']['total'] == 0
    assert p.result['testsets']['tests'][0]['statement_io_validation'] == 'NOT_REQUESTED'
