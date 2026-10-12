import copy
import io
import json
import zipfile
from pathlib import Path
from app.db import Job, LoginCode, SessionLocal, now
from app.schema import empty_document
from app.domain import document_hash, parse_script, three_way_merge
from app.config import get_settings, Settings
import pytest


def save(client, problem, state):
    return client.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})


def test_email_verification_is_one_time_and_creates_account(client):
    requested = client.post("/api/auth/code", json={"email": "first@example.com"})
    assert requested.status_code == 200
    outbox = get_settings().data_dir / "development-mail"
    code = next(json.loads(p.read_text())["code"] for p in sorted(outbox.glob("*.json"), key=lambda p:p.stat().st_mtime_ns, reverse=True) if json.loads(p.read_text())["email"] == "first@example.com")
    verified = client.post("/api/auth/verify", json={"email": "first@example.com", "code": code})
    assert verified.status_code == 200
    assert "httponly" in verified.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").json()["email"] == "first@example.com"
    assert client.post("/api/auth/verify", json={"email": "first@example.com", "code": code}).status_code == 401
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_expired_code_and_attempt_limit(client):
    assert client.post("/api/auth/code", json={"email":"invalid@example.com"}).status_code == 200
    for i in range(5):
        assert client.post("/api/auth/verify", json={"email":"invalid@example.com", "code":"not-six-digits"}).status_code == 422
    with SessionLocal() as db:
        row = db.get(LoginCode, "invalid@example.com")
        row.attempts = 5
        db.commit()
    assert client.post("/api/auth/verify", json={"email":"invalid@example.com", "code":"123456"}).status_code == 401


def test_csrf_and_cross_origin_are_blocked(owner):
    assert owner.post("/api/problems",json={"name":"x","slug":"xx"},headers={"X-Editor-Request":"0"}).status_code==403
    assert owner.post("/api/problems",json={"name":"x","slug":"xx"},headers={"Origin":"https://evil.example"}).status_code==403


def test_personal_working_copy_and_shared_commit(owner, problem, clients):
    reader = clients("reader")
    assert owner.put(f"/api/problems/{problem}/accesses",json={"login":"reader","role":"READ"}).status_code==200
    state=owner.get(f"/api/problems/{problem}").json()
    state["document"]["info"]["name"]="새 제목"
    result=save(owner,problem,state)
    assert result.status_code==200
    assert reader.get(f"/api/problems/{problem}").json()["document"]["info"]["name"]=="두 수의 합"
    assert owner.post(f"/api/problems/{problem}/commit",json={"version":result.json()["version"],"message":"제목 수정"}).status_code==200
    assert reader.get(f"/api/problems/{problem}").json()["document"]["info"]["name"]=="새 제목"


def test_read_only_and_revoked_access_cannot_modify_or_read(owner,problem,clients):
    reader=clients("reader")
    owner.put(f"/api/problems/{problem}/accesses",json={"login":"reader","role":"READ"})
    state=reader.get(f"/api/problems/{problem}").json()
    assert save(reader,problem,state).status_code==403
    assert reader.post(f"/api/problems/{problem}/jobs",json={"kind":"verify","version":0}).status_code==403
    owner.put(f"/api/problems/{problem}/accesses",json={"login":"reader","role":"NONE"})
    assert reader.get(f"/api/problems/{problem}").status_code==404


def test_owner_access_is_protected(owner,problem):
    assert owner.put(f"/api/problems/{problem}/accesses",json={"login":"author","role":"NONE"}).status_code==400


def test_optimistic_version_prevents_lost_save(owner,problem):
    state=owner.get(f"/api/problems/{problem}").json()
    state["document"]["info"]["name"]="첫 번째 저장"
    assert save(owner,problem,state).status_code==200
    state["document"]["info"]["name"]="오래된 저장"
    assert save(owner,problem,state).status_code==409
    assert owner.get(f"/api/problems/{problem}").json()["document"]["info"]["name"]=="첫 번째 저장"


def test_independent_changes_merge_but_same_field_conflicts(owner,problem,clients):
    writer=clients("writer")
    owner.put(f"/api/problems/{problem}/accesses",json={"login":"writer","role":"WRITE"})
    a=owner.get(f"/api/problems/{problem}").json();b=writer.get(f"/api/problems/{problem}").json()
    a["document"]["info"]["name"]="제목1";b["document"]["statements"]["korean"]["legend"]="지문"
    av=save(owner,problem,a).json()["version"];bv=save(writer,problem,b).json()["version"]
    assert owner.post(f"/api/problems/{problem}/commit",json={"version":av,"message":"제목"}).status_code==200
    assert writer.post(f"/api/problems/{problem}/commit",json={"version":bv,"message":"지문"}).status_code==200
    owner.post(f"/api/problems/{problem}/update",json={"version":av+1})
    a=owner.get(f"/api/problems/{problem}").json();b=writer.get(f"/api/problems/{problem}").json()
    a["document"]["info"]["name"]="a";b["document"]["info"]["name"]="b"
    av=save(owner,problem,a).json()["version"];bv=save(writer,problem,b).json()["version"]
    owner.post(f"/api/problems/{problem}/commit",json={"version":av,"message":"a"})
    result=writer.post(f"/api/problems/{problem}/commit",json={"version":bv,"message":"b"})
    assert result.status_code==409
    assert "info/name" in result.json()["error"]["conflicts"]


def test_foreign_blobs_cannot_be_attached(owner,problem,clients):
    stranger=clients("stranger")
    p2=stranger.post("/api/problems",json={"name":"private","slug":"private"}).json()["id"]
    blob=stranger.post(f"/api/problems/{p2}/blobs",content=b"private code").json()
    state=owner.get(f"/api/problems/{problem}").json()
    state["document"]["files"]["secret.py"]={"name":"secret.py","kind":"source","language":"python3",**blob}
    assert save(owner,problem,state).status_code==400
    assert owner.get(f"/api/problems/{p2}/blobs/{blob['sha256']}").status_code==404


def test_reserved_files_and_invalid_time_are_rejected(owner,problem):
    blob=owner.post(f"/api/problems/{problem}/blobs",content=b"fake testlib").json()
    state=owner.get(f"/api/problems/{problem}").json()
    state["document"]["files"]["testlib.h"]={"name":"testlib.h","kind":"resource","language":"text",**blob}
    assert save(owner,problem,state).status_code==422
    state["document"]["files"]={};state["document"]["info"]["time_limit_ms"]=1011
    assert save(owner,problem,state).status_code==422


def test_issue_transitions_and_reader_permissions(owner,problem,clients):
    row=owner.post(f"/api/problems/{problem}/issues",json={"title":"검수 의견","content":"테스트 보강","kind":"ENHANCEMENT"}).json()
    assert owner.patch(f"/api/problems/{problem}/issues/{row['id']}",json={"status":"CLOSED"}).status_code==200
    assert owner.patch(f"/api/problems/{problem}/issues/{row['id']}",json={"comment":"닫힌 이슈"}).status_code==400
    assert owner.patch(f"/api/problems/{problem}/issues/{row['id']}",json={"status":"REOPENED","comment":"재검토"}).status_code==200
    reader=clients("reader");owner.put(f"/api/problems/{problem}/accesses",json={"login":"reader","role":"READ"})
    assert reader.post(f"/api/problems/{problem}/issues",json={"title":"x","content":"x"}).status_code==403


def test_job_snapshot_and_package_commit_requirement(owner,problem):
    state=owner.get(f"/api/problems/{problem}").json();state["document"]["info"]["name"]="변경"
    version=save(owner,problem,state).json()["version"]
    assert owner.post(f"/api/problems/{problem}/jobs",json={"kind":"package","version":version}).status_code==409
    job=owner.post(f"/api/problems/{problem}/jobs",json={"kind":"render","version":version}).json()
    state=owner.get(f"/api/problems/{problem}").json();state["document"]["info"]["name"]="다음 변경";save(owner,problem,state)
    with SessionLocal() as db:
        row=db.get(Job,job["id"])
        assert row.document["info"]["name"]=="변경"
        assert row.document_hash==document_hash(row.document)


def test_deleted_problem_can_be_restored(owner,problem):
    assert owner.patch(f"/api/problems/{problem}/deleted",json={"deleted":True}).status_code==200
    assert owner.get(f"/api/problems/{problem}").status_code==404
    assert owner.get("/api/problems?deleted=true").json()[0]["id"]==problem
    assert owner.patch(f"/api/problems/{problem}/deleted",json={"deleted":False}).status_code==200
    assert owner.get(f"/api/problems/{problem}").status_code==200


def test_generator_script_has_no_shell_execution():
    assert parse_script('gen 100 7 > 1\ngen 100 8 > $')==[{"index":1,"command":"gen 100 7"},{"index":2,"command":"gen 100 8"}]
    with pytest.raises(Exception):parse_script('gen ; rm -rf / > 1')
    with pytest.raises(Exception):parse_script('gen > 1\ngen > 1')


def test_archive_path_traversal_and_xml_entities_rejected(owner):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,"w") as z:z.writestr('../escape','danger')
    result=owner.post('/api/imports?name=x&slug=imported',content=archive.getvalue())
    assert result.status_code==400
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('problem.xml','<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><problem/>')
    assert owner.post('/api/imports?name=x&slug=imported',content=archive.getvalue()).status_code==400


def test_production_rejects_development_authentication():
    with pytest.raises(ValueError):Settings(env='production',mail_mode='console',database_url='sqlite://',public_origin='https://editor.zoj.kr')


def test_explicit_resolution_keeps_chosen_change(owner,problem,clients):
    colleague=clients('colleague')
    owner.put(f'/api/problems/{problem}/accesses',json={'login':'colleague','role':'WRITE'})
    theirs=colleague.get(f'/api/problems/{problem}').json()
    ours=owner.get(f'/api/problems/{problem}').json()
    theirs['document']['info']['name']='동료 제목'
    version=save(colleague,problem,theirs).json()['version']
    assert colleague.post(f'/api/problems/{problem}/commit',json={'version':version,'message':'제목 변경'}).status_code==200
    ours['document']['info']['name']='내 제목'
    version=save(owner,problem,ours).json()['version']
    conflict=owner.post(f'/api/problems/{problem}/update',json={'version':version})
    assert conflict.status_code==409
    result=owner.post(f'/api/problems/{problem}/resolve',json={'version':version,'revision':1,'choices':{'info/name':'theirs'}})
    assert result.status_code==200,result.text
    state=owner.get(f'/api/problems/{problem}').json()
    assert state['document']['info']['name']=='동료 제목'
    assert state['base_revision']==1
    assert owner.post(f'/api/problems/{problem}/commit',json={'version':state['version'],'message':'확인'}).json()['committed'] is False


def test_downgraded_translator_cannot_publish_old_code_changes(owner,problem,clients):
    translator=clients('translator')
    owner.put(f'/api/problems/{problem}/accesses',json={'login':'translator','role':'WRITE'})
    state=translator.get(f'/api/problems/{problem}').json()
    state['document']['info']['name']='번역 외 변경'
    version=save(translator,problem,state).json()['version']
    owner.put(f'/api/problems/{problem}/accesses',json={'login':'translator','role':'READ','translator':True})
    response=translator.post(f'/api/problems/{problem}/commit',json={'version':version,'message':'범위 초과'})
    assert response.status_code==403


def test_malformed_manifest_is_rejected_without_partial_problem(owner):
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('editor.json','[]')
    assert owner.post('/api/imports?name=x&slug=broken',content=archive.getvalue()).status_code==400
    assert not owner.get('/api/problems').json()
