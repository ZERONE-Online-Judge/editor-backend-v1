import copy

import pytest

from app.constraint_comparison import compare_constraints
from app.db import SessionLocal, WorkingCopy
from app.review_report import build
from app.schema import empty_document
from app.storage import blob_path, put_blob


def document(problem, text, source=None, *, field='input', language='cpp17'):
    doc = empty_document('Comparison')
    doc['statements']['korean'][field] = text
    if source is not None:
        with SessionLocal() as db:
            metadata = put_blob(db, problem, source.encode() if isinstance(source, str) else source)
            doc['files']['validator.cpp'] = {'name': 'validator.cpp', 'kind': 'source', 'language': language, **metadata}
            doc['validator'] = 'validator.cpp'
            db.commit()
    return doc


def comparison(doc):
    return compare_constraints(doc)[0]


def test_rich_rows_show_actual_bounds_locations_and_int64_values_without_js_rounding(problem):
    doc = document(problem, '$1 ≤ P ≤ 10^{18}-500$', "int main(){const long long MX=1'000'000'000'000'000'000LL; long long p=inf.readLong(1,MX-500);}")
    result = comparison(doc)
    assert result['version'] == 1 and result['validator'] == {'name': 'validator.cpp', 'language': 'cpp17'}
    assert result['counts'] == {'compared': 1, 'matched': 1, 'different': 0, 'unresolved': 0, 'statement_only': 0, 'validator_only': 0}
    row = result['rows'][0]
    assert row['status'] == 'match' and row['matching'] == 'case'
    assert row['statement'][0]['high'] == row['validator'][0]['high'] == str(10**18 - 500)
    assert row['statement'][0]['location'].startswith('korean · input') and row['validator'][0]['location'] == '1'
    assert '단순 대입문' in row['reason'] and result['limitations']


@pytest.mark.parametrize('text,source,status,matching', [
    ('$1 ≤ N ≤ 100$', 'inf.readInt(1,100,"N");', 'match', 'exact'),
    ('$1 ≤ N ≤ 200$', 'int n=inf.readInt(1,100);', 'difference', 'case'),
    ('$0 ≤ A_i ≤ 100$', 'auto a=inf.readInts(n,0,100);', 'match', 'indexed'),
    ('$0 ≤ A_i ≤ 200$', 'inf.readLongs(n,0,100,"A");', 'difference', 'indexed'),
    ('$0 ≤ A_i ≤ 100$', 'inf.readInt(0,100,"A");', 'statement_only', None),
    ('$1 ≤ N ≤ 100+K$', 'inf.readInt(1,100,"N");', 'unresolved', 'exact'),
    ('$1 ≤ N ≤ 100$', 'if(flag)inf.readInt(1,200,"N");', 'unresolved', 'exact'),
    ('$1 ≤ N ≤ 100$', 'flag && inf.readInt(1,200,"N");', 'unresolved', 'exact'),
])
def test_exact_case_indexed_and_unresolved_matches_are_explained(problem, text, source, status, matching):
    result = comparison(document(problem, text, 'int main(){' + source + '}'))
    row = next(row for row in result['rows'] if row['name'] in {'N', 'A_i'})
    assert row['status'] == status and row['matching'] == matching
    assert row['reason_code'] and row['reason']
    if status != 'difference': assert result['counts']['different'] == 0


@pytest.mark.parametrize('text,source', [
    ('$1 ≤ N ≤ 100$; $1 ≤ n ≤ 200$', 'inf.readInt(1,100,"n");'),
    ('$0 ≤ A_i ≤ 100$; $0 ≤ A_j ≤ 200$', 'inf.readInts(n,0,100,"A");'),
])
def test_nonunique_name_mapping_never_creates_a_difference(problem, text, source):
    result = comparison(document(problem, text, source))
    assert all(row['matching'] == 'exact' for row in result['rows'] if row['status'] == 'difference')
    assert any(row['status'] == 'ambiguous' for row in result['rows'])


def test_conflicting_translations_and_conditional_additional_statement_are_not_differences(problem):
    doc = document(problem, '$1 ≤ N ≤ 200$', 'inf.readInt(1,100,"N");')
    doc['statements']['english'] = {**doc['statements']['korean'], 'input': '$1 ≤ N ≤ 100$'}
    row = comparison(doc)['rows'][0]
    assert row['status'] == 'ambiguous' and len(row['statement']) == 2
    assert not any(item['code'] == 'CONSTRAINT_DIFFERENCE' for item in build(doc)['items'])
    del doc['statements']['english']
    doc['statements']['korean']['notes'] = '부분 문제\n$1 ≤ N ≤ 10$'
    assert comparison(doc)['rows'][0]['status'] == 'unresolved'


def test_common_and_notes_ranges_are_included_but_output_and_tutorial_are_not(problem):
    doc = document(problem, '$1 ≤ N ≤ 100$', 'inf.readInt(1,100,"N");inf.readLong(0,10,"K");', field='notes')
    doc['common_statement'] = '$0 ≤ K ≤ 10$'
    doc['statements']['korean'].update(output='$1 ≤ N ≤ 200$', tutorial='$1 ≤ N ≤ 300$')
    result = comparison(doc)
    assert result['counts']['matched'] == 2 and result['counts']['different'] == 0


@pytest.mark.parametrize('mode,expected', [('missing', 'validator_missing'), ('language', 'validator_language_unsupported'),
    ('bytes', 'validator_unreadable'), ('removed', 'validator_unreadable'), ('large', 'validator_too_large'), ('custom', 'no_validator_ranges')])
def test_unavailable_or_unsupported_validator_never_silently_vanishes(problem, mode, expected):
    source = None if mode == 'missing' else b'\xff' if mode == 'bytes' else 'x' * 262145 if mode == 'large' else 'checkInt(token,1,100);' if mode == 'custom' else 'inf.readInt(1,100,"N");'
    doc = document(problem, '$1 ≤ N ≤ 100$', source, language='python3' if mode == 'language' else 'cpp17')
    if mode == 'removed': blob_path(doc['files']['validator.cpp']['sha256']).unlink()
    result = comparison(doc)
    assert expected in {notice['code'] for notice in result['notices']}
    assert result['counts']['compared'] == 0 and result['counts']['statement_only'] == 1


def test_prose_diagnostic_names_use_assignment_only_and_unnamed_calls_explain_why(problem):
    doc = document(problem, '$1 ≤ Q ≤ 100$', 'int Q=inf.readInt(1,100,"T must be valid");inf.readInt(1,100,"N must be valid");')
    result = comparison(doc)
    assert result['counts']['matched'] == 1 and result['counts']['unresolved'] == 1
    unnamed = next(row for row in result['rows'] if row['status'] == 'unresolved')
    assert unnamed['reason_code'] == 'missing_name' and 'N must be valid' in unnamed['validator'][0]['expression']


def test_row_caps_keep_all_warning_counts_and_prioritize_actionable_rows(problem):
    lines = [f'$1 ≤ N{i} ≤ {100 if i < 200 else 200}$' for i in range(205)]
    source = '\n'.join(f'inf.readInt(1,100,"N{i}");' for i in range(205))
    report = build(document(problem, '\n'.join(lines), source))
    result = report['constraint_comparison']
    assert len(result['rows']) == 200 and result['counts']['compared'] == 205 and result['counts']['different'] == 5
    assert all(row['status'] == 'difference' for row in result['rows'][:5])
    assert sum(item['code'] == 'CONSTRAINT_DIFFERENCE' for item in report['items']) == 5
    assert 'rows_truncated' in {notice['code'] for notice in result['notices']}


def test_bounded_expression_evidence_and_parser_limits_are_explicit(problem):
    doc = document(problem, '$1 ≤ N ≤ ' + '+'.join(['K'] * 400) + '$', 'inf.readInt(1,100,"N");')
    result = comparison(doc)
    assert result['rows'][0]['status'] == 'unresolved'
    assert len(result['rows'][0]['statement'][0]['expression']) <= 512
    assert 'evidence_truncated' in {notice['code'] for notice in result['notices']}
    many = document(problem, '$1 ≤ N ≤ 100$', '\n'.join('inf.readInt(1,100,"N");' for _ in range(513)))
    limited = comparison(many)
    assert 'validator_entry_limit' in {notice['code'] for notice in limited['notices']}
    assert limited['counts']['compared'] == 0 and limited['rows'][0]['reason_code'] == 'analysis_truncated'


def test_api_authorization_cache_and_additive_shape(owner, problem, clients, client):
    doc = document(problem, '$1 ≤ N ≤ 100$', 'int n=inf.readInt(1,100);')
    owner.get(f'/api/problems/{problem}')
    with SessionLocal() as db:
        wc = db.query(WorkingCopy).filter_by(problem_id=problem).one()
        wc.document = copy.deepcopy(doc)
        db.commit()
    endpoint = f'/api/problems/{problem}/review-report'
    first = owner.get(endpoint)
    assert first.status_code == 200
    body = first.json()
    assert body['constraint_comparison']['rows'][0]['validator'][0]['low'] == '1'
    body['constraint_comparison']['rows'].clear()
    assert owner.get(endpoint).json()['constraint_comparison']['rows']
    assert client.get(endpoint).status_code == 401
    assert clients('unrelated').get(endpoint).status_code == 404
