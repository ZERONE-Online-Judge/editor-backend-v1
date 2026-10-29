import base64
import copy
import hashlib
import json
import zipfile

import pytest
from pydantic import ValidationError

from app import judge_bridge as bridge
from app.db import Problem, Revision, SessionLocal
from app.demo import populate
from app.domain import case_order, expanded_tests, ordered_tests
from app.revision_history import changes
from app.schema import TestSet as SchemaSet
from test_invocation_scope import scoped_pipeline
from test_judge_bridge import fixture_data, record
from test_package_export import run_export


def test_order_defaults_to_numbers_and_keeps_generation_identity():
    config = {"tests": {"manual": {"index": 7, "command": "", "input_sha": "a" * 64}},
              "script": 'repeat seed in 1..3\ngen {seed} > $\nend', "groups": {}, "groups_enabled": False}
    assert [t['index'] for t in expanded_tests(config)] == [1, 2, 3, 7]
    config['order'] = [3, 7, 1, 99]
    before = copy.deepcopy(config)
    tests = expanded_tests(config)
    assert [t['index'] for t in tests] == [3, 7, 1, 2]
    assert tests[0]['command'] == 'gen 3'
    assert tests[1]['input_sha'] == 'a' * 64
    assert config == before
    assert SchemaSet().model_dump().get('order') is None


@pytest.mark.parametrize('order', [[1, 1], [0], [1001], [1.5], [True], ['2']])
def test_invalid_orders_are_rejected(order):
    with pytest.raises(ValidationError):
        SchemaSet(order=order)


def test_save_commit_and_reload_preserve_order_without_changing_tests(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        db.commit()
    state = owner.get(f'/api/problems/{problem}').json()
    before = copy.deepcopy(state['document'])
    state['document']['testsets']['tests']['order'] = [3, 1, 2]
    saved = owner.put(f'/api/problems/{problem}/working-copy', json={
        'version': state['version'], 'document': state['document']})
    assert saved.status_code == 200, saved.text
    committed = owner.post(f'/api/problems/{problem}/commit', json={
        'version': saved.json()['version'], 'message': '테스트 실행 순서 변경'})
    assert committed.status_code == 200, committed.text
    after = owner.get(f'/api/problems/{problem}').json()['document']
    assert after['testsets']['tests']['order'] == [3, 1, 2]
    assert after['testsets']['tests']['tests'] == before['testsets']['tests']['tests']
    assert changes(before, after) == [{
        'path': 'testsets/tests/order', 'label': 'tests · 테스트 실행 순서',
        'kind': 'added', 'before': None, 'after': [3, 1, 2]}]


def test_preparation_and_partial_scope_use_configured_order(problem):
    pipeline, _ = scoped_pipeline(problem, test_indices=[1, 3])
    config = pipeline.doc['testsets']['tests']
    config['tests'].update({str(i): {**config['tests']['1'], 'index': i} for i in [2, 3]})
    config['order'] = [3, 2, 1]
    pipeline.doc['info']['skip_duplicate_validation'] = True
    pipeline.prepare()
    assert [t['index'] for t in pipeline.result['testsets']['tests']] == [3, 1]
    assert list(config['tests']) == ['1', '2', '3']


@pytest.mark.parametrize('export_format', ['zoj', 'editor', 'linux'])
def test_exports_and_reimports_keep_order_and_input_answer_pairs(owner, problem, monkeypatch, export_format):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc['testsets']['tests']['order'] = [3, 1, 2]
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    archive, content = run_export(owner, problem, monkeypatch, export_format)
    expected = ordered_tests(doc['testsets']['tests'])
    from app.storage import read_blob
    with archive:
        if export_format == 'zoj':
            cases = json.loads(archive.read('manifest.json'))['testcase_sets'][0]['testcases']
            assert [t['display_order'] for t in cases] == [1, 2, 3]
            for test, case in zip(expected, cases):
                assert archive.read(case['input']) == read_blob(test['input_sha'])
                assert archive.read(case['output']) == read_blob(test['answer_sha'])
        else:
            for ordinal, test in enumerate(expected, 1):
                assert archive.read(f'tests/tests/{ordinal:03d}') == read_blob(test['input_sha'])
                assert archive.read(f'tests/tests/{ordinal:03d}.a') == read_blob(test['answer_sha'])
            assert json.loads(archive.read('editor.json'))['document']['testsets']['tests']['order'] == [3, 1, 2]
    response = owner.post('/api/imports?name=Reordered&slug=reordered-' + export_format, content=content)
    assert response.status_code == 201, response.text
    restored = owner.get('/api/problems/' + response.json()['id']).json()['document']['testsets']['tests']
    assert restored['order'] == [3, 1, 2]
    assert restored['tests'] == doc['testsets']['tests']['tests']


def test_judge_wire_positions_restore_sparse_original_ids_and_output_files(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path)
    original = pipeline.result['testsets']['tests']
    tests = [{**original[2], 'index': 9}, {**original[0], 'index': 2}, {**original[1], 'index': 5}]
    path, _ = bridge.archive(pipeline, 'main.cpp', 'tests', tests, 0)
    with zipfile.ZipFile(path) as archive:
        cases = json.loads(archive.read('manifest.json'))['testcases']
    assert [case['index'] for case in cases] == [1, 2, 3]
    assert [case['input_sha'] for case in cases] == [test['input_sha'] for test in tests]
    outputs = [b'first', b'second', b'third']
    run = record(statuses=['wrong_answer', 'accepted', 'time_limit_exceeded'])
    for case, content in zip(run['result']['testcase_results'], outputs):
        case.update(output_available=True, output_sha256=hashlib.sha256(content).hexdigest(), output_size=len(content))
    requests = []
    def remote(method, url, *args, **kwargs):
        index = int(url.split('/')[-2])
        requests.append(index)
        return {'base64': base64.b64encode(outputs[index - 1]).decode()}
    monkeypatch.setattr(bridge, 'remote', remote)
    pipeline.blob = lambda content: hashlib.sha256(content).hexdigest()
    rows = bridge.observed_cases(pipeline, None, 'tests', tests, run)
    assert requests == [1, 2, 3]
    assert [row['index'] for row in rows] == [9, 2, 5]
    assert [row['verdict'] for row in rows] == ['WA', 'AC', 'TLE']
    assert [row['actual_sha'] for row in rows] == [hashlib.sha256(output).hexdigest() for output in outputs]
    assert bridge.wire_tests([tests[1], tests[2]]) == {2: tests[1], 5: tests[2]}


def test_reordered_verification_reports_original_ids_in_execution_order(problem, tmp_path, monkeypatch):
    pipeline = fixture_data(problem, tmp_path)
    pipeline.doc['testsets']['tests']['order'] = [3, 1, 2]
    pipeline.result['testsets']['tests'] = ordered_tests(pipeline.doc['testsets']['tests'])
    def run_batch(pipeline, user, name, set_name, tests, attempt, cancellation):
        assert [test['index'] for test in tests] == [3, 1, 2]
        value = record()
        return {**value, **value['result'], 'cases': bridge.observed_cases(pipeline, user, set_name, tests, value)}
    monkeypatch.setattr(bridge, 'run_batch', run_batch)
    result = bridge.verify_solution(pipeline, None, 'main.cpp', None)
    assert result['matched'] is True
    assert [case['index'] for case in result['cases']] == [3, 1, 2]


def test_case_order_preserves_legacy_set_labels_and_custom_order_within_each_set():
    document = {'testsets': {'tests': {'order': [3, 1, 2]}, 'samples': {}}}
    cases = [{'testset': name, 'index': index} for name, index in [('tests', 1), ('tests', 2), ('samples', 1), ('tests', 3)]]
    assert [(case['testset'], case['index']) for case in sorted(cases, key=case_order(document))] == [('samples', 1), ('tests', 3), ('tests', 1), ('tests', 2)]
