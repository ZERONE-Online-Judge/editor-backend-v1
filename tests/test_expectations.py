import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.expectations import VERSION, evaluate, expected_tag
from app.schema import Document, empty_document
from app.packages import descriptor


def fixture():
    doc = empty_document("Scoped verdicts")
    group = {"points_policy": "COMPLETE_GROUP", "feedback_policy": "COMPLETE", "dependencies": []}
    tests = {str(i): {"index": i, "command": f"gen {i}", "input_sha": None, "answer_sha": None,
                     "group": name, "points": 0, "description": "", "sample": False}
             for i, name in enumerate(["small", "medium", "large"], 1)}
    doc["testsets"]["tests"].update(tests=tests, groups_enabled=True, groups={name: group for name in ["small", "medium", "large"]})
    source = {"name": "small.cpp", "kind": "source", "language": "cpp17", "sha256": "a" * 64, "size": 1,
              "tag": "TL", "extra_tags": {}, "group_tags": {"tests": {"small": "OK", "medium": "OK", "large": "TL"}}}
    doc["solutions"] = {source["name"]: source}
    tests = list(tests.values())
    rows = [{"testset": "tests", "index": i, "verdict": verdict} for i, verdict in enumerate(["AC", "AC", "TLE"], 1)]
    return doc, source, {"tests": tests}, rows


def multiple_default_sets(counts=(90, 31)):
    doc, source, _, _ = fixture()
    source['group_tags'] = {}
    template = copy.deepcopy(doc['testsets']['tests']['tests']['1'])
    doc['testsets'] = {
        name: {'script': '', 'groups_enabled': False, 'groups': {}, 'tests': {
            str(index): {**template, 'index': index, 'group': ''} for index in range(1, count + 1)
        }} for name, count in zip(['tests', 'tle'], counts)
    }
    prepared = {name: list(config['tests'].values()) for name, config in doc['testsets'].items()}
    rows = [{'testset': name, 'index': test['index'], 'verdict': 'AC'} for name, tests in prepared.items() for test in tests]
    rows[-1]['verdict'] = 'TLE'
    return doc, source, prepared, rows


def test_default_tl_matches_ac_in_90_tests_and_tle_in_31_tests():
    doc, source, prepared, rows = multiple_default_sets()
    result = evaluate(doc, source, prepared, rows)
    assert result['matched'] and result['expectations_complete']
    assert len(result['expectations']) == 1
    scope = result['expectations'][0]
    assert scope['testsets'] == ['tests', 'tle']
    assert scope['origin'] == 'default' and scope['tag'] == 'TL'
    assert scope['tested'] == scope['total'] == 121
    assert scope['verdicts'] == ['AC', 'TLE']
    assert result['expectations_version'] == VERSION


@pytest.mark.parametrize('last_verdict,matched', [('AC', False), ('TLE', True), ('JUDGE_ERROR', False), ('CE', False)])
def test_global_expectation_keeps_real_mismatches_and_judge_errors(last_verdict, matched):
    doc, source, prepared, rows = multiple_default_sets((1, 1))
    rows[-1]['verdict'] = last_verdict
    assert evaluate(doc, source, prepared, rows)['matched'] is matched


@pytest.mark.parametrize('explicit,matched', [('OK', True), ('TL', False)])
def test_explicit_testset_expectations_remain_independent(explicit, matched):
    doc, source, prepared, rows = multiple_default_sets()
    source['extra_tags'] = {'tests': explicit, 'tle': 'TL'}
    result = evaluate(doc, source, prepared, rows)
    assert result['matched'] is matched and result['expectations_complete']
    assert len(result['expectations']) == 2
    assert all(scope['origin'] == 'testset' for scope in result['expectations'])


def test_group_overrides_are_separate_from_combined_default_scope():
    doc, source, prepared, rows = multiple_default_sets((2, 1))
    doc['testsets']['tests']['tests']['1']['group'] = 'small'
    source['group_tags'] = {'tests': {'small': 'OK'}}
    result = evaluate(doc, source, prepared, rows)
    assert result['matched'] and result['expectations_complete']
    scopes = {scope['origin']: scope for scope in result['expectations']}
    assert scopes['group']['tested'] == scopes['group']['total'] == 1
    assert scopes['default']['tested'] == scopes['default']['total'] == 2
    assert scopes['default']['testsets'] == ['tests', 'tle']


def test_same_index_in_two_sets_does_not_hide_unexecuted_tests():
    doc, source, prepared, rows = multiple_default_sets((1, 1))
    prepared['tle'] = []
    result = evaluate(doc, source, prepared, rows[:1])
    scope = result['expectations'][0]
    assert scope['tested'] == 1 and scope['total'] == 2
    assert scope['matched'] is None and not result['expectations_complete']


@pytest.mark.parametrize('kind', ['verify', 'package'])
def test_saved_report_refresh_changes_only_derived_verification(kind, owner, problem, monkeypatch):
    from app.db import Job, Problem, SessionLocal
    doc, source, prepared, rows = multiple_default_sets()
    for row in rows:
        row.update(cpu_ms=37, memory_kb=1024)
    saved = {'solutions': [{'name': source['name'], 'tag': 'TL', 'matched': False,
                            'complete': True, 'skipped': False, 'cases': rows}],
             'testsets': prepared, 'verified': False, 'mismatches': [source['name']]}
    with SessionLocal() as db:
        row = Job(problem_id=problem, user_id=db.get(Problem, problem).owner_id, kind=kind,
                  status='completed', document=doc, document_hash='a' * 64,
                  workspace_version=0, revision=0, result=copy.deepcopy(saved))
        db.add(row)
        db.commit()
        job_id = row.id
    monkeypatch.setattr('app.judge_bridge.remote', lambda *args, **kwargs: pytest.fail('Saved results must not trigger judging'))
    response = owner.get(f'/api/problems/{problem}/jobs/{job_id}')
    assert response.status_code == 200
    result = response.json()['result']
    assert result['verified'] is (kind == 'verify')
    assert result['solutions'][0]['matched'] is (kind == 'verify')
    assert result['solutions'][0]['cases'] == rows
    with SessionLocal() as db:
        assert db.get(Job, job_id).result == saved


def test_groups_override_global_default_without_requiring_tle_in_small_groups():
    doc, source, prepared, rows = fixture()
    result = evaluate(doc, source, prepared, rows)
    assert result["matched"] and result["expectations_complete"]
    assert [(scope["group"], scope["tag"], scope["matched"]) for scope in result["expectations"]] == [
        ("small", "OK", True), ("medium", "OK", True), ("large", "TL", True)]
    rows[-1]["verdict"] = "AC"
    assert not evaluate(doc, source, prepared, rows)["matched"]


def test_partial_group_execution_never_claims_full_expectation_verification():
    doc, source, prepared, rows = fixture()
    prepared["tests"] = [prepared["tests"][-1]]
    doc["testsets"]["tests"]["tests"]["4"] = {**prepared["tests"][0], "index": 4}
    rows = [{"testset": "tests", "index": 3, "verdict": "AC"}]
    result = evaluate(doc, source, prepared, rows)
    assert result["matched"] and not result["expectations_complete"]
    assert result["expectations"][0]["matched"] is None
    rows[0]["verdict"] = "JUDGE_ERROR"
    assert not evaluate(doc, source, prepared, rows)["matched"]


def test_testset_names_are_not_misread_as_legacy_group_names():
    source = {"tag": "OK", "extra_tags": {"samples": "TL"}}
    assert expected_tag(source, "tests", "samples", ["tests", "samples"])[0] == "OK"
    assert expected_tag(source, "samples", "", ["tests", "samples"])[0] == "TL"


def test_nr_group_can_be_skipped_without_fabricating_case_results():
    doc, source, prepared, rows = fixture()
    source["group_tags"]["tests"]["large"] = "NR"
    result = evaluate(doc, source, prepared, rows[:2])
    assert result["matched"] and result["expectations_complete"]
    assert result["expectations"][-1]["skipped"]
    assert result["expectations"][-1]["tested"] == 0


@pytest.mark.parametrize("groups", [{"missing": {"small": "OK"}}, {"tests": {"missing": "OK"}}, {"tests": {"small": "MA"}}])
def test_invalid_group_or_tag_references_are_rejected(groups):
    doc, source, _, _ = fixture()
    source["group_tags"] = groups
    with pytest.raises(ValidationError):
        Document.model_validate(doc)


def test_legacy_document_hash_shape_and_exported_scopes_are_preserved():
    doc, source, prepared, _ = fixture()
    parsed = Document.model_validate(doc).model_dump()
    assert parsed["solutions"][source["name"]]["group_tags"] == source["group_tags"]
    job = SimpleNamespace(document=doc, revision=1, problem_id="demo")
    xml = descriptor(job, prepared)
    assert b'group="small" tag="OK"' in xml
    old = copy.deepcopy(source)
    del old["group_tags"]
    from app.schema import Solution
    assert "group_tags" not in Solution.model_validate(old).model_dump()


def test_readiness_recognizes_scoped_wrong_verdicts_and_ignores_nr_solutions():
    from app.domain import cautions
    doc, source, _, _ = fixture()
    source['tag']='OK'
    assert 'NO_WRONG_SOLUTIONS' not in {c['code'] for c in cautions(doc)}
    source['tag']='NR'
    assert 'NO_WRONG_SOLUTIONS' in {c['code'] for c in cautions(doc)}
