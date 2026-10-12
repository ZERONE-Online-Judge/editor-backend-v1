import os
import pytest
from app.db import Job, Problem, Revision, SessionLocal
from app.demo import populate
from app.pipeline import Pipeline
from app.packages import build_package
from app.worker import claim_job, process_job

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
