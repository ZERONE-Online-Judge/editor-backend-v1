"""Worker completion and domain correctness are independent, saved evidence."""
import copy
from types import SimpleNamespace

import pytest
from app.job_outcomes import summarize
from app.job_errors import AssessmentFailure, failure_result


def outcome(kind='verify', status='completed', result=None, options=None):
    return summarize(SimpleNamespace(kind=kind, status=status, options=options or {}), result or {})


def solution(matched=True, complete=True, verdict='WA', state='completed'):
    return {'name': 'wrong.cpp', 'tag': 'WA', 'matched': matched, 'complete': complete,
            'state': state, 'cases': [{'index': 1, 'verdict': verdict}],
            'expectations_complete': complete,
            'expectations': [{'matched': matched, 'complete': complete, 'skipped': False}]}


def test_expected_wrong_answer_is_a_successful_check_not_an_execution_failure():
    result = {'verified': True, 'solutions': [solution()]}
    original = copy.deepcopy(result)
    value = outcome(result=result)
    assert value['execution'] == 'completed'
    assert value['assessment']['status'] == 'passed'
    assert value['assessment']['counts'] == {'total': 1, 'passed': 1, 'failed': 0, 'unconfirmed': 0}
    assert result == original


def test_normal_completion_can_have_a_mismatched_expected_verdict():
    value = outcome(result={'verified': False, 'solutions': [solution(False, verdict='AC')]})
    assert value['execution'] == 'completed'
    assert value['assessment']['status'] == 'failed'
    assert value['assessment']['code'] == 'expectations_differ'


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_pending_placeholder_mismatches_are_not_final_failures(status):
    value = outcome(status=status, result={'verified': False, 'solutions': [solution(False, state='queued')]})
    assert value['assessment']['status'] == 'pending'


def test_interrupted_solution_does_not_turn_its_false_placeholder_into_a_mismatch():
    value = outcome(status='failed', result={'solutions': [
        {'tag': 'OK', 'matched': False, 'state': 'queued', 'total_tests': 3, 'cases': []}]})
    assert value['assessment']['status'] == 'incomplete'
    assert value['assessment']['counts']['failed'] == 0


def test_partial_expected_scope_is_not_a_full_pass():
    row = solution(True, complete=False, verdict='AC')
    row['expectations'][0]['matched'] = None
    value = outcome(result={'verified': False, 'solutions': [row]})
    assert value['assessment']['status'] == 'incomplete'
    assert value['assessment']['counts']['unconfirmed'] == 1


def test_compile_failure_is_a_mismatch_but_unavailable_judging_is_unconfirmed():
    row = solution(False, complete=False, verdict='CE')
    row['expectations'][0]['matched'] = None
    assert outcome(result={'solutions': [row]})['assessment']['status'] == 'failed'
    row['cases'][0]['verdict'] = 'JUDGE_ERROR'
    row['expectations'][0]['matched'] = False
    value = outcome(result={'solutions': [row]})['assessment']
    assert value['status'] == 'incomplete'
    assert value['counts']['failed'] == 0 and value['counts']['unconfirmed'] == 1


def test_confirmed_mismatch_is_retained_even_when_another_solution_is_unfinished():
    value = outcome(status='cancelled', result={'solutions': [solution(False),
        {'tag': 'OK', 'matched': False, 'state': 'queued', 'total_tests': 3, 'cases': []}]})
    assert value['execution'] == 'cancelled'
    assert value['assessment']['status'] == 'failed'
    assert value['assessment']['partial']
    assert value['assessment']['counts']['failed'] == 1
    assert value['assessment']['counts']['unconfirmed'] == 1


def test_all_skipped_solutions_and_missing_records_are_never_passed():
    assert outcome(result={'verified': True, 'solutions': [{'tag': 'NR', 'skipped': True, 'matched': True}]})['assessment']['status'] == 'empty'
    assert outcome(result={'progress': 100, 'summary': '완료'})['assessment']['status'] == 'unknown'


@pytest.mark.parametrize('kind', ['validator-tests', 'checker-tests'])
def test_component_expected_rejections_and_crashes_can_match(kind):
    for actual in ['INVALID', 'CRASHED', 'WRONG_ANSWER']:
        result = {'unit_tests': [{'expected': actual, 'actual': actual, 'matched': True}],
                  'verified': True, 'unit_test_totals': {kind: 1}}
        assert outcome(kind, result=result)['assessment']['status'] == 'passed'
        result['unit_tests'][0]['matched'] = False
        result['verified'] = False
        value = outcome(kind, result=result)
        assert value['execution'] == 'completed' and value['assessment']['status'] == 'failed'


def test_empty_or_partial_self_tests_do_not_pass():
    assert outcome('validator-tests', result={'unit_tests': [], 'verified': False})['assessment']['status'] == 'empty'
    value = outcome('validator-tests', result={'unit_tests': [{'matched': True}],
                    'verified': True, 'unit_test_totals': {'validator-tests': 2}})
    assert value['assessment']['status'] == 'incomplete'


@pytest.mark.parametrize('mode', ['inputs', 'validate', 'full', 'answers'])
def test_preparation_modes_check_actual_evidence(mode):
    result = {'preparation_total': 1, 'testsets': {'tests': [{'validator': 'VALID', 'answer_sha': 'a'}]}}
    value = outcome('prepare', result=result, options={'prepare_mode': mode})
    assert value['assessment']['status'] == 'passed'
    result['preparation_total'] = 2
    assert outcome('prepare', result=result, options={'prepare_mode': mode})['assessment']['status'] == 'incomplete'
    if mode in {'full', 'answers'}:
        result['preparation_total'] = 1
        result['testsets']['tests'][0]['answer_sha'] = None
        assert outcome('prepare', result=result, options={'prepare_mode': mode})['assessment']['status'] != 'passed'


def test_cancellation_keeps_preparation_partial_and_input_failure_is_explicit():
    result = {'preparation_total': 1, 'testsets': {'tests': [{'validator': 'VALID'}]}}
    assert outcome('prepare', status='cancelled', result=result, options={'prepare_mode': 'validate'})['assessment']['status'] == 'incomplete'
    error = AssessmentFailure('tests #1: 입력 조건 위반', 'input', 'input_rejected')
    failure = failure_result(error)
    assert failure['error'] == str(error)
    value = outcome('prepare', status='failed', result=failure)
    assert value['execution'] == 'failed'
    assert value['assessment']['status'] == 'failed'
    assert value['assessment']['code'] == 'input_rejected'
    assert outcome('prepare', status='failed', result=failure_result(RuntimeError('network')))['assessment']['status'] == 'unknown'


def test_stress_discovery_is_not_treated_as_a_failed_verification():
    found = outcome('stress', result={'stress': {'iterations_completed': 10, 'counterexample_count': 2}, 'uncovered_candidates': ['other']})
    assert found['execution'] == 'completed' and found['assessment']['status'] == 'found'
    assert found['assessment']['found'] == 2 and found['assessment']['uncovered'] == 1
    assert outcome('stress', result={'stress': {'iterations_completed': 10, 'counterexample_count': 0}})['assessment']['status'] == 'not_found'
    assert outcome('stress', status='failed', result={'stress': {'iterations_completed': 1}})['assessment']['status'] == 'incomplete'
    assert outcome('stress', status='cancelled', result={'counterexamples': [{'id': 'one'}]})['assessment']['partial']


def test_unverified_packages_are_explicitly_not_checked():
    value = outcome('package', result={'verified': False, 'verification_skipped': True}, options={'verify': False})
    assert value['execution'] == 'completed'
    assert value['assessment']['status'] == 'not_run'
    result = {'verified': False, 'solutions': [solution()], 'unit_tests': [{'matched': False}]}
    assert outcome('package', status='failed', result=result)['assessment']['status'] == 'failed'


def test_package_export_failure_can_follow_successful_checks_but_legacy_partial_tests_are_unknown():
    result = {'verified': True, 'solutions': [solution()], 'unit_tests': [{'matched': True}]}
    assert outcome('package', status='failed', result=result)['assessment']['status'] == 'incomplete'
    result['unit_test_totals'] = {'validator-tests': 1, 'checker-tests': 1}
    assert outcome('package', status='failed', result=result)['assessment']['status'] == 'incomplete'
    result['unit_tests'].append({'matched': True})
    value = outcome('package', status='failed', result=result)
    assert value['execution'] == 'failed' and value['assessment']['status'] == 'passed'


def test_render_completion_can_include_failed_pdf_generation():
    result = {'statements': {'ko': {'html': 'OK', 'pdf': 'FAILED'}}}
    value = outcome('render', result=result)
    assert value['execution'] == 'completed' and value['assessment']['status'] == 'failed'
    result['statements']['ko']['pdf'] = 'OK'
    assert outcome('render', result=result)['assessment']['status'] == 'passed'


def test_list_detail_and_legacy_expectations_share_the_request_snapshot(owner, problem, clients):
    from app.db import SessionLocal, Job, Problem
    from test_expectations import multiple_default_sets
    document, source, prepared, cases = multiple_default_sets((1, 1))
    result = {'solutions': [{'name': source['name'], 'tag': 'TL', 'matched': False, 'complete': True, 'cases': cases}],
              'testsets': prepared, 'verified': False, 'summary': '기존 판정'}
    with SessionLocal() as db:
        model = db.get(Problem, problem)
        actor = model.owner_id
        # Current materials deliberately disagree with the execution snapshot.
        model.document = copy.deepcopy(document)
        model.document['solutions'][source['name']]['tag'] = 'OK'
        for number in range(2):
            db.add(Job(id=f'{number + 1:032x}', problem_id=problem, user_id=actor,
                kind='verify', status='completed', document=copy.deepcopy(document), document_hash='d' * 64,
                workspace_version=1, revision=0, options={}, result=copy.deepcopy(result)))
        db.commit()
    rows = owner.get(f'/api/problems/{problem}/jobs?page=1&page_size=10').json()['items']
    assert len(rows) == 2
    for row in rows:
        detail = owner.get(f'/api/problems/{problem}/jobs/{row["id"]}').json()
        assert row['outcome'] == detail['outcome']
        assert row['outcome']['assessment']['status'] == 'passed'
        assert set(row['result']) == {'summary', 'progress'}
        assert 'document' not in row
    with SessionLocal() as db:
        saved = db.get(Job, rows[0]['id'])
        assert saved.result == result and saved.status == 'completed'
    assert clients('outsider').get(f'/api/problems/{problem}/jobs?page=1').status_code in {403, 404}
