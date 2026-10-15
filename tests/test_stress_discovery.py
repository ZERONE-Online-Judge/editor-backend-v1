import copy
import hashlib
from types import SimpleNamespace

import pytest

from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.stress import generator_command, minimum_cover, validate_patterns
from app.stress_minimizer import CounterexampleMinimizer
from test_stress_invocations import stress_pipeline, counterexample_job


def test_range_draws_are_bounded_deterministic_and_pattern_specific():
    template = 'gen [1..10] [-100..-50] [8..8] --seed={seed}'
    draws = []
    command = generator_command(template, 987654, 2, draws)
    assert command == generator_command(template, 987654, 2)
    assert command.endswith('--seed=987654')
    assert [value['expression'] for value in draws] == ['[1..10]', '[-100..-50]', '[8..8]']
    assert 1 <= draws[0]['value'] <= 10 and -100 <= draws[1]['value'] <= -50 and draws[2]['value'] == 8
    assert len({generator_command(template, seed, index) for seed in range(20) for index in range(2)}) > 20


def ready(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(row.document)
        db.commit()
    return owner.get(f'/api/problems/{problem}').json()


@pytest.mark.parametrize('command', ['gen [10..1]', 'gen [0..9223372036854775808]', 'gen [1..2.5]', 'gen [1..10', 'gen "unfinished', 'missing 1', 'gen 1 | gen 2'])
def test_invalid_patterns_are_rejected_before_queueing(owner, problem, command):
    state = ready(owner, problem)
    response = owner.post(f'/api/problems/{problem}/jobs', json={'kind': 'stress', 'version': state['version'], 'options': {
        'generator': command, 'candidates': ['wrong.py'],
    }})
    assert response.status_code == 400, response.text
    assert response.json()['error']['code'] == 'invalid_stress_pattern'
    assert owner.get(f'/api/problems/{problem}/jobs').json() == []


def test_pattern_scope_group_and_seed_are_frozen_in_request(owner, problem):
    state = ready(owner, problem)
    state['document']['testsets']['tests'].update(groups_enabled=True, groups={'small': {'points_policy': 'COMPLETE_GROUP', 'feedback_policy': 'COMPLETE', 'dependencies': []}})
    saved = owner.put(f'/api/problems/{problem}/working-copy', json={'version': state['version'], 'document': state['document']})
    request = {'kind': 'stress', 'version': saved.json()['version'], 'options': {
        'patterns': [{'name': 'small', 'command': 'gen [1..10] {seed}'}, {'name': 'large', 'command': 'gen [50..100] {seed}'}],
        'candidates': ['wrong.py'], 'group': 'small', 'seed_start': 42, 'seed_step': 3,
        'stop_mode': 'all_candidates', 'minimize': True,
    }}
    response = owner.post(f'/api/problems/{problem}/jobs', json=request)
    assert response.status_code == 202, response.text
    config = response.json()['options']['stress_configuration']
    assert config['focus_uncovered'] and config['stop_mode'] == 'all_candidates'
    assert config['group'] == 'small' and config['seed_start'] == 42 and config['seed_step'] == 3
    assert config['patterns'] == request['options']['patterns'] and config['minimize']
    request['options']['group'] = 'missing'
    assert owner.post(f'/api/problems/{problem}/jobs', json=request).json()['error']['code'] == 'invalid_stress_group'
    request['options'].update(group='small', seed_start=9007199254740991, iterations=2)
    assert owner.post(f'/api/problems/{problem}/jobs', json=request).json()['error']['code'] == 'invalid_stress_seed'


def test_all_solution_mode_focuses_only_uncovered_solutions_and_ignores_target_one(monkeypatch):
    pipeline, calls, _ = stress_pipeline(monkeypatch, candidates=['wrong', 'slow'], iterations=10, stop_mode='all_candidates', counterexample_limit=1,
                                         patterns=[{'name': 'odd', 'command': 'gen {seed}'}, {'name': 'even', 'command': 'gen {seed}'}])
    original = pipeline.run_solution
    def compare(name, content, answer=b''):
        if name == 'slow' and content != b'gen 3\n':
            calls[name].append(content)
            return 'AC', {'output': b'42', 'stderr': '', 'cpu_ms': 1, 'memory_kb': 1}
        return original(name, content, answer)
    pipeline.run_solution = compare
    pipeline.stress()
    assert len(calls['wrong']) == 1 and len(calls['slow']) == 3
    assert pipeline.result['stress']['stop_reason'] == 'all_candidates_covered'
    assert pipeline.result['uncovered_candidates'] == []
    assert len(pipeline.result['counterexamples']) == 2
    stats = pipeline.result['pattern_summaries']
    assert (stats[0]['attempts'], stats[0]['compared_inputs'], stats[0]['counterexample_inputs']) == (2, 2, 2)
    assert (stats[1]['attempts'], stats[1]['counterexample_inputs']) == (1, 0)
    assert pipeline.result['coverage']['method'] == 'exact'
    assert len(pipeline.result['coverage']['example_ids']) == 2
    assert {(row['solution'], row['verdict']) for row in pipeline.result['counterexample_categories']} == {('wrong', 'WA'), ('slow', 'TLE')}


def test_unbroken_solutions_remain_explicit_when_the_iteration_budget_expires(monkeypatch):
    pipeline, calls, _ = stress_pipeline(monkeypatch, candidates=['wrong', 'slow'], stop_mode='all_candidates')
    original = pipeline.run_solution
    def compare(name, content, answer=b''):
        if name == 'slow':
            calls[name].append(content)
            return 'AC', {'output': b'42', 'stderr': '', 'cpu_ms': 1, 'memory_kb': 1}
        return original(name, content, answer)
    pipeline.run_solution = compare
    pipeline.stress()
    assert pipeline.result['stress']['stop_reason'] == 'iterations'
    assert pipeline.result['uncovered_candidates'] == ['slow']
    assert len(calls['wrong']) == 1 and len(calls['slow']) == 3


def test_evidence_budget_never_leaves_an_empty_unverified_counterexample(monkeypatch):
    monkeypatch.setattr('app.stress_search.MAX_STRESS_EVIDENCE_BYTES', 1)
    pipeline, _, _ = stress_pipeline(monkeypatch)
    pipeline.stress()
    assert pipeline.result['stress']['stop_reason'] == 'evidence_limit'
    assert pipeline.result['counterexamples'] == []


def example(identifier, *pairs):
    return {'id': identifier, 'input_bytes': len(identifier), 'findings': [{'solution': name, 'verdict': verdict} for name, verdict in pairs]}


def test_minimum_cover_preserves_observed_solution_verdict_pairs():
    rows = [example('a', ('one', 'WA')), example('b', ('two', 'TLE')), example('c', ('one', 'WA'), ('two', 'TLE')), example('d', ('one', 'RE'))]
    result = minimum_cover(rows)
    assert result['method'] == 'exact' and set(result['example_ids']) == {'c', 'd'}
    assert result['covered_pairs'] == 3
    assert minimum_cover([])['example_ids'] == []
    large = rows + [example(str(i), ('one', 'WA')) for i in range(20)]
    assert minimum_cover(large)['method'] == 'greedy'
    assert len(minimum_cover(large)['example_ids']) == 2


def minimizer(content=b'6\n-10 -12 -3 -5 -9 -7\n', **options):
    blobs = {}
    def blob(value):
        sha = hashlib.sha256(value).hexdigest()
        blobs[sha] = value
        return sha
    def validator(value, *args):
        try:
            values = list(map(int, value.split()))
            return 1 <= values[0] == len(values) - 1 <= 20 and all(-100 <= x <= 100 for x in values[1:]), {}
        except (ValueError, IndexError):
            return False, {}
    def execute(name, value, answer=b''):
        values = list(map(int, value.split()))[1:]
        output = str(max(values)).encode() if name == 'main' else b'0'
        return 'AC', {'output': output, 'stderr': '', 'cpu_ms': 1, 'memory_kb': 1}
    pipeline = SimpleNamespace(job=SimpleNamespace(options={'testset': 'tests', 'minimize_attempts': 100, 'minimize_seconds': 10, **options}),
                               sandbox=SimpleNamespace(check_cancelled=lambda: None), blob=blob, validator=validator, run_solution=execute,
                               checker=lambda inp, out, ans: ('AC' if out == ans else 'WA', {'stderr': ''}), event=lambda *args, **kwargs: None)
    row = {'input_sha': blob(content), 'findings': [{'solution': 'wrong', 'tag': 'WA', 'verdict': 'WA', 'actual_sha': blob(b'0')}]}
    return CounterexampleMinimizer(pipeline, row, content, 'main', 1000000), row, blobs


def test_array_reduction_preserves_validator_and_all_observed_failures_without_replacing_original():
    reducer, row, blobs = minimizer()
    original = row['input_sha']
    reducer.reduce()
    reduced = row['minimized']
    assert blobs[reduced['input_sha']] == b'1\n-1\n'
    assert blobs[reduced['answer_sha']] == b'-1'
    assert reduced['findings'][0]['verdict'] == 'WA'
    assert row['input_sha'] == original and blobs[original].startswith(b'6\n')
    assert row['minimization']['accepted_steps'] > 0 and row['minimization']['status'] == 'finished'


def test_reduction_will_not_trade_away_a_second_solutions_failure():
    reducer, row, blobs = minimizer()
    row['findings'].append({**row['findings'][0], 'solution': 'wrong2'})
    execute = reducer.p.run_solution
    def compare(name, content, answer=b''):
        if name == 'wrong2' and int(content.split()[0]) < 2:
            return 'AC', {'output': answer, 'stderr': '', 'cpu_ms': 1, 'memory_kb': 1}
        return execute(name, content, answer)
    reducer.p.run_solution = compare
    reducer.reduce()
    assert int(blobs[row['minimized']['input_sha']].split()[0]) == 2
    assert {finding['solution'] for finding in row['minimized']['findings']} == {'wrong', 'wrong2'}


def test_reduction_budget_and_input_limit_are_visible():
    reducer, row, _ = minimizer(minimize_attempts=1)
    reducer.reduce()
    assert row['minimization']['attempts'] == 1 and row['minimization']['status'] == 'budget'
    reducer, row, _ = minimizer(content=b'0' * (1024 * 1024 + 1))
    reducer.reduce()
    assert row['minimization']['status'] == 'input_limit' and 'minimized' not in row


def test_resource_failures_require_a_second_matching_verdict_during_reduction():
    reducer, row, _ = minimizer(minimize_attempts=4)
    row['findings'][0]['verdict'] = 'TLE'
    execute = reducer.p.run_solution
    wrong_calls = []
    def unstable(name, content, answer=b''):
        if name == 'wrong':
            wrong_calls.append(content)
            return ('TLE' if len(wrong_calls) % 2 else 'AC'), {'output': answer, 'stderr': '', 'cpu_ms': 1, 'memory_kb': 1}
        return execute(name, content, answer)
    reducer.p.run_solution = unstable
    reducer.reduce()
    assert len(wrong_calls) >= 2 and 'minimized' not in row


def test_interruption_keeps_already_verified_reductions_and_restores_cancellation():
    reducer, row, blobs = minimizer()
    def check():
        if row['minimization']['accepted_steps']:
            raise RuntimeError('cancelled')
    reducer.p.sandbox.check_cancelled = check
    with pytest.raises(RuntimeError, match='cancelled'):
        reducer.reduce()
    assert reducer.p.sandbox.check_cancelled is check
    assert row['minimization']['status'] == 'interrupted'
    assert row['minimized']['findings'][0]['verdict'] == 'WA'
    assert len(blobs[row['minimized']['input_sha']]) < len(reducer.original)


def test_group_registration_uses_verified_reduction_without_overwriting_original(owner, problem):
    state = owner.get(f'/api/problems/{problem}').json()
    state['document']['testsets']['tests'].update(groups_enabled=True, groups={'small': {'points_policy': 'COMPLETE_GROUP', 'feedback_policy': 'COMPLETE', 'dependencies': []}})
    owner.put(f'/api/problems/{problem}/working-copy', json={'version': state['version'], 'document': state['document']})
    job_id, state, examples = counterexample_job(owner, problem)
    with SessionLocal() as db:
        from app.storage import put_blob
        row = db.get(Job, job_id)
        result = copy.deepcopy(row.result)
        result['counterexamples'][0]['findings'] = [{'solution': 'wrong', 'verdict': 'WA'}]
        result['counterexamples'][0]['minimized'] = {'input_sha': put_blob(db, problem, b'1\n-1\n')['sha256'],
            'answer_sha': put_blob(db, problem, b'-1\n')['sha256'], 'findings': [{'solution': 'wrong', 'verdict': 'WA'}]}
        reduced_sha = result['counterexamples'][0]['minimized']['input_sha']
        row.result = result
        db.commit()
    response = owner.post(f'/api/problems/{problem}/jobs/{job_id}/counterexamples/apply', json={
        'version': state['version'], 'testset': 'tests', 'group': 'small', 'use_minimized': True, 'counterexample_ids': [examples[0]['id']],
    })
    assert response.status_code == 200, response.text
    assert response.json()['group'] == 'small' and response.json()['needs_context_validation']
    tests = owner.get(f'/api/problems/{problem}').json()['document']['testsets']['tests']['tests']
    assert list(tests.values())[0]['input_sha'] == reduced_sha and list(tests.values())[0]['group'] == 'small'
    with SessionLocal() as db:
        assert db.get(Job, job_id).result['counterexamples'][0]['input_sha'] == examples[0]['input_sha']


def test_unverified_reduced_findings_or_unknown_groups_cannot_be_registered(owner, problem):
    job_id, state, examples = counterexample_job(owner, problem)
    with SessionLocal() as db:
        row = db.get(Job, job_id)
        result = copy.deepcopy(row.result)
        result['counterexamples'][0].update(findings=[{'solution': 'one', 'verdict': 'WA'}, {'solution': 'two', 'verdict': 'RE'}],
            minimized={'input_sha': examples[0]['input_sha'], 'answer_sha': examples[0]['answer_sha'], 'findings': [{'solution': 'one', 'verdict': 'WA'}]})
        row.result = result
        db.commit()
    values = {'version': state['version'], 'testset': 'tests', 'use_minimized': True, 'counterexample_ids': [examples[0]['id']]}
    path = f'/api/problems/{problem}/jobs/{job_id}/counterexamples/apply'
    assert owner.post(path, json=values).json()['error']['code'] == 'invalid_minimized_counterexample'
    values.update(use_minimized=False, group='missing')
    assert owner.post(path, json=values).json()['error']['code'] == 'invalid_test_group'
    assert owner.get(f'/api/problems/{problem}').json()['document']['testsets']['tests']['tests'] == {}
