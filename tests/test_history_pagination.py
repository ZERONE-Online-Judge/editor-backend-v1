import copy
import pytest
from app.db import SessionLocal, Job, JobApplication, Problem, Publication, Revision, User
from app.domain import document_hash


def seed_jobs(owner, problem, count=125, actor=None):
    state = owner.get(f'/api/problems/{problem}').json()
    with SessionLocal() as db:
        creator = db.get(Problem, problem).owner_id
        for index in range(count):
            db.add(Job(id=f'{index + 1:032x}', problem_id=problem, user_id=actor or creator,
                kind=['verify', 'prepare', 'stress', 'package', 'render', 'checker-tests', 'validator-tests'][index % 7],
                status='completed', document=copy.deepcopy(state['document']), document_hash='d' * 64,
                workspace_version=0, revision=0, created_at=100,
                options={'source_document_hash': state['document_hash'] if index % 2 == 0 else 'e' * 64},
                result={'summary': f'작업 {index}'}))
        db.commit()
    return state['document_hash'], creator


def test_all_history_pages_preserve_tied_order_and_requesters(owner, problem, clients):
    collaborator = clients('reviewer')
    with SessionLocal() as db:
        actor = db.query(User).filter_by(handle='reviewer').one().id
    assert owner.put(f'/api/problems/{problem}/accesses', json={'login': 'reviewer', 'role': 'READ'}).status_code == 200
    current_hash, _ = seed_jobs(owner, problem, actor=actor)
    seen = []
    for page in range(1, 8):
        response = owner.get(f'/api/problems/{problem}/jobs', params={'page': page, 'page_size': 20, 'current_hash': current_hash})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value['total'] == 125 and value['pages'] == 7
        assert value['counts'] == {'all': 125, 'current': 63, 'other': 62}
        assert len(value['items']) == (5 if page == 7 else 20)
        seen.extend(row['id'] for row in value['items'])
        for row in value['items']:
            assert row['requester'] == {'id': actor, 'display_name': 'reviewer', 'handle': 'reviewer'}
            assert 'email' not in row['requester']
            assert 'document' not in row
    assert seen == [f'{index:032x}' for index in range(125, 0, -1)]
    detail = collaborator.get(f'/api/problems/{problem}/jobs/{seen[0]}').json()
    assert detail['requester']['id'] == actor
    assert owner.get(f'/api/problems/{problem}/jobs', params={'page': 999}).json()['page'] == 7


def test_material_filter_uses_source_hash_and_only_viewers_applications(owner, problem, clients):
    other = clients('other')
    current_hash, creator = seed_jobs(owner, problem, 6)
    with SessionLocal() as db:
        other_id = db.query(User).filter_by(handle='other').one().id
        db.add(JobApplication(job_id=f'{2:032x}', user_id=creator, document_hash=current_hash, workspace_version=1))
        db.add(JobApplication(job_id=f'{4:032x}', user_id=other_id, document_hash=current_hash, workspace_version=1))
        db.commit()
    params = {'page': 1, 'current_hash': current_hash, 'relation': 'current'}
    current = owner.get(f'/api/problems/{problem}/jobs', params=params).json()
    assert current['total'] == 4
    assert {row['id'] for row in current['items']} == {f'{n:032x}' for n in [1, 2, 3, 5]}
    assert current['counts'] == {'all': 6, 'current': 4, 'other': 2}
    other_rows = owner.get(f'/api/problems/{problem}/jobs', params={**params, 'relation': 'other'}).json()
    assert {row['id'] for row in other_rows['items']} == {f'{n:032x}' for n in [4, 6]}
    assert other.get(f'/api/problems/{problem}/jobs', params=params).status_code in {403, 404}


@pytest.mark.parametrize('kind', ['verify', 'prepare', 'stress', 'package', 'render', 'checker-tests', 'validator-tests'])
def test_kind_filter_happens_before_pagination(owner, problem, kind):
    seed_jobs(owner, problem, 35)
    value = owner.get(f'/api/problems/{problem}/jobs', params={'page': 2, 'page_size': 3, 'kind': kind}).json()
    assert value['total'] == 5 and len(value['items']) == 2
    assert all(row['kind'] == kind for row in value['items'])


@pytest.mark.parametrize('params', [{'page': 0}, {'page': -1}, {'page_size': 101}, {'page_size': 0},
                                    {'kind': 'unknown'}, {'current_hash': 'invalid'}, {'relation': 'unknown'}])
def test_invalid_page_parameters_are_rejected(owner, problem, params):
    assert owner.get(f'/api/problems/{problem}/jobs', params=params).status_code == 422


def test_relation_filter_requires_hash_and_empty_pages_are_valid(owner, problem):
    assert owner.get(f'/api/problems/{problem}/jobs', params={'page': 1, 'relation': 'current'}).status_code == 400
    result = owner.get(f'/api/problems/{problem}/jobs', params={'page': 7}).json()
    assert result == {'items': [], 'total': 0, 'page': 1, 'page_size': 20, 'pages': 1, 'counts': None}


def test_revision_history_is_paged_with_authors_and_snapshot_hashes(owner, problem, clients):
    state = owner.get(f'/api/problems/{problem}').json()
    with SessionLocal() as db:
        author = db.get(Problem, problem).owner_id
        for number in range(1, 43):
            db.add(Revision(problem_id=problem, number=number, author_id=author, message=f'변경 {number}', document=copy.deepcopy(state['document'])))
        db.commit()
    result = owner.get(f'/api/problems/{problem}/revisions', params={'page': 3}).json()
    assert result['total'] == 43 and [row['number'] for row in result['items']] == [2, 1, 0]
    assert all(row['author'] == 'author' and row['hash'] == document_hash(state['document']) for row in result['items'])
    assert clients('outsider').get(f'/api/problems/{problem}/revisions?page=1').status_code in {403, 404}


def test_publication_pages_exceed_legacy_limit_and_exclude_unfinished_transfers(owner, problem):
    _, actor = seed_jobs(owner, problem, 1)
    with SessionLocal() as db:
        for index in range(126):
            db.add(Publication(operation_id=f'{index + 1:032x}', problem_id=problem, user_id=actor, job_id=f'{1:032x}',
                contest_id='contest', division_id='division', problem_code='A', created_at=100,
                result={'problem_id': 'remote', 'title': '제목', 'revision': 0, 'url': 'https://zoj.kr'} if index < 125 else {}))
        db.commit()
    result = owner.get(f'/api/problems/{problem}/zoj-publications', params={'page': 7}).json()
    assert result['total'] == 125 and len(result['items']) == 5
    assert result['items'][-1]['operation_id'] == f'{1:032x}'
    assert all(row['requester']['id'] == actor for row in result['items'])
