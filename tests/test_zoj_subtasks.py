import copy
import itertools
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import judge_bridge as bridge
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.domain import document_hash
from app.schema import empty_document
from app.scoring import score_results
from app.zoj_package import build_zoj_package, check_compatibility
from app.zoj_subtasks import export_subtasks
from app.config import get_settings
from test_judge_bridge import fixture_data, record


def point_document(groups, tests):
    doc = empty_document('Subtasks')
    doc['info']['points_enabled'] = True
    doc['testsets'] = {'tests': {'groups_enabled': bool(groups), 'groups': groups, 'script': '',
                                'tests': {str(t['index']): t for t in tests}}}
    return doc


def group(policy='COMPLETE_GROUP', dependencies=()):
    return {'points_policy': policy, 'feedback_policy': 'COMPLETE', 'dependencies': list(dependencies)}


def case(index, points, name=''):
    return {'index': index, 'points': points, 'group': name}


def entries(doc):
    return [(name, test, order) for order, (name, test) in enumerate(
        ((name, test) for name, config in doc['testsets'].items() for test in config['tests'].values()), 1)]


def assert_equivalent_for_every_submission(doc):
    source = entries(doc)
    exported = export_subtasks(doc, source)
    prepared = {name: list(config['tests'].values()) for name, config in doc['testsets'].items()}
    for passed in itertools.product([False, True], repeat=len(source)):
        rows = [{'testset': name, 'index': test['index'], 'group': test['group'],
                 'verdict': 'AC' if accepted else 'WA', 'score': test['points'] if accepted else 0}
                for (name, test, order), accepted in zip(source, passed)]
        expected = score_results(doc, prepared, rows)['total_score']
        statuses = {order: accepted for (_, _, order), accepted in zip(source, passed)}
        actual = sum(task['points'] for task in exported if all(statuses[order] for order in task['testcase_orders']))
        assert actual == expected, (passed, exported)
    return exported


def test_complete_groups_each_test_and_transitive_dependencies_preserve_every_score():
    doc = point_document({'small': group(), 'medium': group('EACH_TEST', ['small']),
                          'large': group(dependencies=['small', 'medium'])},
                         [case(1, 10, 'small'), case(2, 10, 'small'), case(3, 15, 'medium'),
                          case(4, 15, 'medium'), case(5, 25, 'large'), case(6, 25, 'large')])
    result = assert_equivalent_for_every_submission(doc)
    assert [t['points'] for t in result] == [20, 15, 15, 50]
    assert result[-1]['testcase_orders'] == [1, 2, 3, 4, 5, 6]
    assert all(len(t['subtask_id']) <= 64 and len(t['name']) <= 120 for t in result)


def test_zero_point_dependency_is_shared_without_adding_new_scoring_conditions():
    doc = point_document({'common': group(), 'small': group(dependencies=['common']),
                          'large': group(dependencies=['common'])},
                         [case(1, 0, 'common'), case(2, 30, 'small'), case(3, 70, 'large')])
    result = assert_equivalent_for_every_submission(doc)
    assert [task['testcase_orders'] for task in result] == [[1, 2], [1, 3]]


def test_ungrouped_per_test_points_preserve_each_test_semantics():
    doc = point_document({}, [case(1, 30), case(2, 70)])
    assert [task['points'] for task in assert_equivalent_for_every_submission(doc)] == [30, 70]


def test_merged_sets_reordered_indexes_and_repeated_names_have_stable_unique_ids():
    doc = point_document({'same': group()}, [case(40, 30, 'same')])
    doc['testsets']['other'] = {'groups_enabled': True, 'groups': {'same': group()}, 'script': '',
                               'tests': {'2': case(2, 70, 'same')}}
    result = assert_equivalent_for_every_submission(doc)
    assert len({task['subtask_id'] for task in result}) == 2
    assert [task['testcase_orders'] for task in result] == [[1], [2]]
    assert {task['name'] for task in result} == {'tests / same', 'other / same'}
    reordered = export_subtasks(doc, [(name, test, 10 - order) for name, test, order in entries(doc)])
    assert [task['subtask_id'] for task in reordered] == [task['subtask_id'] for task in result]


def test_fractional_case_weights_are_exact_when_the_complete_group_sum_is_integer():
    doc = point_document({'small': group(), 'large': group()},
                         [case(1, .25, 'small'), case(2, 29.75, 'small'), case(3, 70, 'large')])
    assert [task['points'] for task in assert_equivalent_for_every_submission(doc)] == [30, 70]


@pytest.mark.parametrize('kind,expected', [('percent', '백분율'), ('total', '100점'),
                                          ('fraction', '정수'), ('unscored', '0점 테스트'),
                                          ('empty_dependency', '테스트가 있어야')])
def test_unsupported_semantics_are_explained_instead_of_silently_changed(kind, expected):
    doc = point_document({'small': group(), 'large': group()}, [case(1, 30, 'small'), case(2, 70, 'large')])
    if kind == 'percent': doc['info']['checker_percent'] = True
    if kind == 'total': doc['testsets']['tests']['tests']['1']['points'] = 20
    if kind == 'fraction':
        doc['testsets']['tests']['groups']['small']['points_policy'] = 'EACH_TEST'
        doc['testsets']['tests']['tests']['1']['points'] = 30.5
        doc['testsets']['tests']['tests']['2']['points'] = 69.5
    if kind == 'unscored': doc['testsets']['tests']['tests']['3'] = case(3, 0)
    if kind == 'empty_dependency':
        doc['testsets']['tests']['groups']['empty'] = group()
        doc['testsets']['tests']['groups']['small']['dependencies'] = ['empty']
    with pytest.raises(ValueError, match=expected): export_subtasks(doc, entries(doc))


def test_ioi_package_exports_native_metadata_and_keeps_editor_roundtrip(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc['info']['points_enabled'] = True
        config = doc['testsets']['tests']
        config.update(groups_enabled=True, groups={'small': group(), 'large': group()})
        for test in config['tests'].values():
            test.update(group='small' if test['index'] < 3 else 'large',
                        points=15 if test['index'] < 3 else 70)
        job = Job(problem_id=problem, user_id=row.owner_id, kind='package', status='completed', document=doc,
                  document_hash=document_hash(doc), workspace_version=0, revision=0,
                  options={'export_format': 'zoj', 'testset': 'tests', 'statement_language': 'korean', 'verify': True})
        db.add(job); db.flush()
        check_compatibility(doc, job.options)
        build_zoj_package(job, {'tests': list(config['tests'].values())})
        archive_path = get_settings().data_dir / 'packages' / (job.id + '.zip')
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        active = manifest['testcase_sets'][0]
        assert [t['points'] for t in active['subtasks']] == [30, 70]
        assert [t['testcase_orders'] for t in active['subtasks']] == [[1, 2], [3]]
        assert manifest['version'] == 1
    restored = owner.post('/api/imports?name=Returned&slug=subtasks-returned', content=archive_path.read_bytes())
    assert restored.status_code == 201, restored.text
    document = owner.get('/api/problems/' + restored.json()['id']).json()['document']
    assert document['info']['points_enabled'] and document['testsets']['tests']['groups'] == config['groups']
    assert [test['points'] for test in document['testsets']['tests']['tests'].values()] == [15, 15, 70]
    if os.environ.get('ZOJ_SUBTASK_ARTIFACT'):
        Path(os.environ['ZOJ_SUBTASK_ARTIFACT']).write_bytes(archive_path.read_bytes())


def test_package_merges_same_named_groups_without_merging_their_scores(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc['info']['points_enabled'] = True
        doc['testsets']['other'] = copy.deepcopy(doc['testsets']['tests'])
        prepared = {}
        for set_name, total in [('tests', 30), ('other', 70)]:
            config = doc['testsets'][set_name]
            config.update(groups_enabled=True, groups={'same': group()}, order=[3, 1, 2])
            for test in config['tests'].values():
                test.update(group='same', points=total if test['index'] == 1 else 0)
            prepared[set_name] = [config['tests'][str(index)] for index in config['order']]
        job = Job(problem_id=problem, user_id=row.owner_id, kind='package', status='completed', document=doc,
                  document_hash=document_hash(doc), workspace_version=0, revision=0,
                  options={'export_format': 'zoj', 'testset': 'tests', 'testsets': ['tests', 'other'],
                           'statement_language': 'korean', 'verify': True})
        db.add(job); db.flush()
        check_compatibility(doc, job.options)
        build_zoj_package(job, prepared)
        archive_path = get_settings().data_dir / 'packages' / (job.id + '.zip')
    with zipfile.ZipFile(archive_path) as archive:
        active = json.loads(archive.read('manifest.json'))['testcase_sets'][0]
        assert [test['display_order'] for test in active['testcases']] == [1, 2, 3, 4, 5, 6]
        assert [task['testcase_orders'] for task in active['subtasks']] == [[1, 2, 3], [4, 5, 6]]
        assert [task['points'] for task in active['subtasks']] == [30, 70]
        assert len({task['subtask_id'] for task in active['subtasks']}) == 2


def test_remote_verification_reports_source_submission_score_after_a_failed_group(problem, tmp_path, monkeypatch):
    p = fixture_data(problem, tmp_path, tag='WA')
    p.doc['info']['points_enabled'] = True
    config = p.doc['testsets']['tests']
    config.update(groups_enabled=True, groups={'small': group(), 'large': group()})
    for test in p.result['testsets']['tests']:
        test.update(group='small' if test['index'] < 3 else 'large', points=15 if test['index'] < 3 else 70)
    bridge.compatible(p.doc, p.job.options, 'verify')
    monkeypatch.setattr(bridge, 'remote', lambda *a, **kw: record('wrong_answer', 1))
    result = bridge.verify_solution(p, object(), 'main.cpp', threading.Event())
    assert result['total_score'] == 70 and result['max_score'] == 100 and result['score_scope_complete']
    assert [row['score'] for row in result['cases']] == [0, 15, 70]
    assert [(row['name'], row['score']) for row in result['groups']] == [('small', 0), ('large', 70)]
    path, _ = bridge.archive(p, 'main.cpp', 'tests', p.result['testsets']['tests'], 0)
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read('manifest.json'))['scoring_mode'] == 'ioi'
    p.reference_output = True
    path, _ = bridge.archive(p, 'main.cpp', 'tests', p.result['testsets']['tests'][:1], 1)
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['reference_output'] and 'scoring_mode' not in manifest


def test_remote_partial_scope_or_judge_error_does_not_claim_a_complete_score(problem, tmp_path, monkeypatch):
    p = fixture_data(problem, tmp_path)
    p.doc['info']['points_enabled'] = True
    for test in p.result['testsets']['tests']: test['points'] = 30 if test['index'] < 3 else 40
    monkeypatch.setattr(bridge, 'remote', lambda *a, **kw: record('system_error', 2))
    result = bridge.verify_solution(p, object(), 'main.cpp', threading.Event())
    assert result['total_score'] is None and not result['score_scope_complete']
    tests = p.result['testsets']['tests']
    p.result['testsets']['tests'] = tests[:1]
    p.job.options['test_indices'] = [1]
    value = record(statuses=['accepted'])
    value['result']['testcase_results'] = value['result']['testcase_results'][:1]
    monkeypatch.setattr(bridge, 'remote', lambda *a, **kw: value)
    result = bridge.verify_solution(p, object(), 'main.cpp', threading.Event())
    assert result['total_score'] is None and not result['score_scope_complete']
    assert result['max_score'] == 100


def test_points_verification_admission_requires_a_capable_agent(owner, problem, monkeypatch):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        document = copy.deepcopy(row.document)
        document['info']['points_enabled'] = True
        for test in document['testsets']['tests']['tests'].values():
            test['points'] = 30 if test['index'] < 3 else 40
        row.document = document
        db.get(Revision, (problem, 0)).document = document
        db.commit()
    state = owner.get(f'/api/problems/{problem}').json()
    request = {'kind': 'verify', 'version': state['version'], 'options': {'execution_backend': 'zoj'}}
    rejected = owner.post(f'/api/problems/{problem}/jobs', json=request)
    assert rejected.status_code == 503 and rejected.json()['error']['code'] == 'subtask_agents_unavailable'
    monkeypatch.setattr(bridge, 'capabilities', lambda user: {
        'runtime_revision': 'subtasks-v1', 'subtask_protocol': 'zoj-judge-subtasks-v1', 'subtask_online_nodes': 1,
        'reference_output_protocol': 'editor-reference-output-v1', 'reference_output_online_nodes': 1,
        'execution': {'protocol': 'editor-execute-v1', 'online_nodes': 1, 'runtime_revision': 'execution-v1'}})
    accepted = owner.post(f'/api/problems/{problem}/jobs', json=request)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['options']['judge_revision'] == 'subtasks-v1'
