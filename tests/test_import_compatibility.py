import copy
import io
import json
import zipfile
import xml.etree.ElementTree as ET

import pytest

from sqlalchemy import select
from app.db import Access, SessionLocal, User
from app.domain import references
from app.storage import read_blob
from test_polygon_import import package


def imported(owner):
    _, files = package()
    root = ET.fromstring(files['problem.xml'])
    root.find('assets/solutions/solution[@tag="wrong-answer"]/source').set('type', 'cpp.g++20')
    checker = root.find('assets/checker')
    checker.set('name', 'custom')
    checker.find('source').set('type', 'cpp.g++20')
    root.find('assets/validators/validator/source').set('type', 'cpp.g++20')
    root.find('files/executables/executable/source[@path="files/validate.cpp"]').set('type', 'cpp.g++20')
    files['problem.xml'] = ET.tostring(root)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    response = owner.post('/api/imports?name=Imported&slug=compatibility', content=output.getvalue())
    assert response.status_code == 201, response.text
    return response.json()


def apply(owner, result, changes):
    return owner.post(f"/api/problems/{result['id']}/import-language-settings", json={
        'document_hash': result['document_hash'], 'changes': changes})


def test_import_offers_changes_only_for_judged_solutions_and_active_checker(owner):
    result = imported(owner)
    plan = result['import_report']['zoj_compatibility']
    assert {(row['kind'], row['name'], row['from_language'], row['to_language']) for row in plan['changes']} == {
        ('solution', 'wrong.cpp', 'cpp20', 'cpp17'), ('checker', 'check.cpp', 'cpp20', 'cpp17')}
    assert not plan['issues']
    doc = owner.get(f"/api/problems/{result['id']}/revisions/0").json()
    assert doc['solutions']['wrong.cpp']['language'] == 'cpp20'
    assert doc['files']['check.cpp']['language'] == 'cpp20'
    assert doc['files']['gen.cpp']['language'] == doc['files']['validate.cpp']['language'] == 'cpp20'


def test_confirmed_subset_creates_revision_and_preserves_sources_and_original(owner):
    result = imported(owner)
    url = f"/api/problems/{result['id']}"
    before = owner.get(url).json()
    response = apply(owner, result, [{'kind': 'solution', 'name': 'wrong.cpp', 'language': 'cpp17'}])
    assert response.status_code == 200, response.text
    assert response.json() == {'revision': 1, 'applied': 1}
    after = owner.get(url).json()
    assert after['problem']['revision'] == after['base_revision'] == 1
    expected = copy.deepcopy(before['document'])
    expected['solutions']['wrong.cpp']['language'] = 'cpp17'
    assert after['document'] == expected
    assert owner.get(url + '/revisions/0').json() == before['document']
    assert owner.get(url + '/revisions/1').json() == expected
    assert references(expected) == references(before['document'])
    assert after['document']['files']['check.cpp']['language'] == 'cpp20'
    assert apply(owner, result, [{'kind': 'checker', 'name': 'check.cpp', 'language': 'cpp17'}]).status_code == 409


@pytest.mark.parametrize('changes', [
    [{'kind': 'solution', 'name': 'wrong.cpp', 'language': 'java8'}],
    [{'kind': 'checker', 'name': 'gen.cpp', 'language': 'cpp17'}],
    [{'kind': 'solution', 'name': 'wrong.cpp', 'language': 'cpp17'}] * 2,
])
def test_unoffered_or_duplicate_changes_do_not_modify_the_import(owner, changes):
    result = imported(owner)
    response = apply(owner, result, changes)
    assert response.status_code == 400, response.text
    assert owner.get(f"/api/problems/{result['id']}").json()['problem']['revision'] == 0


def test_import_changes_respect_permissions_and_reject_stale_snapshots(owner, clients):
    result = imported(owner)
    reader = clients('reader')
    with SessionLocal() as db:
        reader_id = db.scalar(select(User.id).where(User.email == 'reader@example.com'))
        db.add(Access(problem_id=result['id'], user_id=reader_id, role='READ'))
        db.commit()
    changes = [{'kind': 'checker', 'name': 'check.cpp', 'language': 'cpp17'}]
    assert apply(reader, result, changes).status_code == 403
    assert apply(clients('outsider'), result, changes).status_code == 404
    assert apply(owner, {**result, 'document_hash': '0' * 64}, changes).status_code == 409
    url = f"/api/problems/{result['id']}"
    state = owner.get(url).json()
    state['document']['info']['name'] = 'Work in progress'
    saved = owner.put(url + '/working-copy', json={'version': state['version'], 'document': state['document']})
    assert saved.status_code == 200, saved.text
    assert apply(owner, result, changes).status_code == 409
    assert owner.get(url).json()['document']['info']['name'] == 'Work in progress'


def test_native_editor_import_has_the_same_plan_and_unconvertible_checker_is_explicit(owner):
    result = imported(owner)
    doc = owner.get(f"/api/problems/{result['id']}/revisions/0").json()
    doc['files']['check.cpp']['language'] = 'java17'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('editor.json', json.dumps({'format': 'zoj-editor-v1', 'document': doc}))
        for sha in references(doc):
            archive.writestr('blobs/' + sha, read_blob(sha))
    response = owner.post('/api/imports?name=Native&slug=native', content=output.getvalue())
    assert response.status_code == 201, response.text
    plan = response.json()['import_report']['zoj_compatibility']
    checker = next(row for row in plan['changes'] if row['kind'] == 'checker')
    assert checker['from_language'] == 'java17' and checker['to_language'] is None
