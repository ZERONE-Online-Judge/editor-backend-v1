import os
import copy
import time
import pytest
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.pipeline import Pipeline
from app.packages import build_package
from app.worker import claim_job, process_job
from app.storage import put_blob

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(os.environ.get("RUN_SANDBOX_TESTS") != "1", reason="Real Editor runtime images required")]


def prepare_problem(problem):
    with SessionLocal() as db:
        row=db.get(Problem,problem)
        row.document=populate(db,row)
        db.get(Revision,(problem,0)).document=row.document
        db.commit()


def test_real_checker_validator_main_wrong_solution_and_package_roundtrip(owner,problem):
    prepare_problem(problem)
    state=owner.get(f'/api/problems/{problem}').json()
    response=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'package','version':state['version']})
    assert response.status_code==202,response.text
    created=response.json()
    claimed=claim_job()
    assert claimed.id==created['id']
    process_job(claimed)
    result=owner.get(f'/api/problems/{problem}/jobs/{created["id"]}').json()
    assert result['status']=='completed',result
    solutions={s['name']:s for s in result['result']['solutions']}
    assert solutions['main.cpp']['matched'] and all(c['verdict']=='AC' for c in solutions['main.cpp']['cases'])
    assert solutions['wrong.py']['matched'] and any(c['verdict']=='WA' for c in solutions['wrong.py']['cases'])
    archive=owner.get(f'/api/problems/{problem}/packages/{created["id"]}')
    assert archive.status_code==200
    imported=owner.post('/api/imports?name=Roundtrip&slug=roundtrip',content=archive.content)
    assert imported.status_code==201, imported.text
    document=owner.get(f'/api/problems/{imported.json()["id"]}').json()['document']
    assert document['solutions']['main.cpp']['tag']=='MA'
    assert document['validator']=='validator.cpp'
    assert len(document['testsets']['tests']['tests'])==3


def test_stress_counterexample_validated_and_persisted(owner,problem):
    prepare_problem(problem)
    state=owner.get(f'/api/problems/{problem}').json()
    result=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'stress','version':state['version'],'options':{'generator':'gen {seed}','reference':'main.cpp','candidate':'wrong.py','iterations':3}})
    assert result.status_code==202,result.text
    process_job(claim_job())
    job=owner.get(f'/api/problems/{problem}/jobs/{result.json()["id"]}').json()
    assert job['status']=='completed',job
    assert job['result']['counterexample']['verdict']=='WA'
    assert job['result']['counterexample']['input_sha']


def test_korean_math_pdf_is_rendered_offline(owner,problem):
    prepare_problem(problem)
    state=owner.get(f'/api/problems/{problem}').json()
    created=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'render','version':state['version']}).json()
    process_job(claim_job())
    row=owner.get(f'/api/problems/{problem}/jobs/{created["id"]}').json()
    assert row['status']=='completed',row
    assert row['result']['statements']['korean']['pdf']=='OK',row['result']['statements']['korean'].get('message')
    pdf=owner.get(f'/api/problems/{problem}/rendered/{created["id"]}/korean.pdf')
    assert pdf.status_code==200 and pdf.content.startswith(b'%PDF-')
    html=owner.get(f'/api/problems/{problem}/rendered/{created["id"]}/korean.html')
    assert html.status_code==200 and b'data:font/' in html.content


def test_real_verified_zoj_zip_can_restore_editor_project(owner,problem):
    prepare_problem(problem)
    state=owner.get(f'/api/problems/{problem}').json()
    created=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'package','version':state['version'],'options':{'export_format':'zoj','statement_language':'korean'}})
    assert created.status_code==202,created.text
    process_job(claim_job())
    job=owner.get(f'/api/problems/{problem}/jobs/{created.json()["id"]}').json()
    assert job['status']=='completed',job
    assert job['result']['verified'] and job['result']['package']['format']=='zoj'
    archive=owner.get(f'/api/problems/{problem}/packages/{job["id"]}')
    result=owner.post('/api/imports?name=Restored&slug=zoj-restored',content=archive.content)
    assert result.status_code==201,result.text
    doc=owner.get('/api/problems/'+result.json()['id']).json()['document']
    assert doc['files']==state['document']['files'] and doc['solutions']==state['document']['solutions']
    # Optional artifact allows the independently versioned ZOJ receiver to check
    # its real parser without importing its Python package into this service.
    if os.environ.get('ZOJ_COMPAT_ARTIFACT'):
        from pathlib import Path
        Path(os.environ['ZOJ_COMPAT_ARTIFACT']).write_bytes(archive.content)


def test_real_generation_auto_apply_and_role_package_roundtrip(owner, problem):
    prepare_problem(problem)
    state = owner.get(f"/api/problems/{problem}").json()
    state["document"]["files"]["validator.cpp"]["roles"] = ["validator"]
    state["document"]["files"]["gen.cpp"]["roles"] = ["generator"]
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    assert saved.status_code == 200, saved.text
    prepared = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "prepare", "version": saved.json()["version"], "options": {"auto_apply": True}})
    assert prepared.status_code == 202, prepared.text
    process_job(claim_job())
    result = owner.get(f"/api/problems/{problem}/jobs/{prepared.json()['id']}").json()
    assert result["status"] == "completed", result
    assert result["result"]["auto_apply"]["status"] == "applied", result
    assert result["application"]
    state = owner.get(f"/api/problems/{problem}").json()
    assert state["pending_preparations"] == []
    assert owner.post(f"/api/problems/{problem}/commit", json={"version": state["version"], "message": "생성 결과 반영"}).status_code == 200
    state = owner.get(f"/api/problems/{problem}").json()
    created = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "package", "version": state["version"]})
    assert created.status_code == 202, created.text
    process_job(claim_job())
    archive = owner.get(f"/api/problems/{problem}/packages/{created.json()['id']}")
    assert archive.status_code == 200, archive.text
    imported = owner.post('/api/imports?name=RoleRoundtrip&slug=role-roundtrip', content=archive.content)
    assert imported.status_code == 201, imported.text
    doc = owner.get('/api/problems/' + imported.json()['id']).json()['document']
    assert doc['files']['validator.cpp']['roles'] == ['validator']
    assert doc['files']['gen.cpp']['roles'] == ['generator']
    assert doc['solutions']['main.cpp']['tag'] == 'MA'


def test_real_stress_shares_inputs_and_collects_multiple_solution_failures(owner, problem):
    prepare_problem(problem)
    state = owner.get(f"/api/problems/{problem}").json()
    duplicate = copy.deepcopy(state["document"]["solutions"]["wrong.py"])
    duplicate["name"] = "wrong2.py"
    state["document"]["solutions"]["wrong2.py"] = duplicate
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    requested = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "stress", "version": saved.json()["version"], "options": {
        "generator": "gen {seed}", "reference": "main.cpp", "candidates": ["wrong.py", "wrong2.py"],
        "iterations": 3, "stop_on_first": False, "counterexample_limit": 5,
    }})
    assert requested.status_code == 202, requested.text
    process_job(claim_job())
    job = owner.get(f"/api/problems/{problem}/jobs/{requested.json()['id']}").json()
    assert job["status"] == "completed", job
    assert len(job["result"]["counterexamples"]) > 1
    assert all([finding["verdict"] for finding in ce["findings"]] == ["WA", "WA"] for ce in job["result"]["counterexamples"])
    assert all(row["tested"] == 3 for row in job["result"]["candidate_summaries"])


def test_real_stress_duration_stops_an_active_program(owner, problem):
    prepare_problem(problem)
    state = owner.get(f"/api/problems/{problem}").json()
    with SessionLocal() as db:
        blob = put_blob(db, problem, b"while True: pass\n")
        db.commit()
    state["document"]["solutions"]["slow.py"] = {"name": "slow.py", "kind": "source", "language": "python3", "tag": "TL", "extra_tags": {}, **blob}
    state["document"]["info"]["time_limit_ms"] = 15000
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    requested = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "stress", "version": saved.json()["version"], "options": {
        "generator": "gen {seed}", "reference": "main.cpp", "candidate": "slow.py", "iterations": 1000,
        "duration_seconds": 2, "stop_on_first": False, "counterexample_limit": 5,
    }})
    assert requested.status_code == 202, requested.text
    started = time.monotonic()
    process_job(claim_job())
    elapsed = time.monotonic() - started
    job = owner.get(f"/api/problems/{problem}/jobs/{requested.json()['id']}").json()
    assert job["status"] == "completed", job
    assert job["result"]["stress"]["stop_reason"] == "duration"
    assert elapsed < 8, elapsed
