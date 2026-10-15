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


def test_real_range_patterns_reduce_and_register_validator_approved_array_counterexamples(owner, problem):
    from app.storage import read_blob
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        sources = {
            'gen.cpp': '#include "testlib.h"\n#include <iostream>\nint main(int argc,char** argv){registerGen(argc,argv,1);int n=opt<int>(1);std::cout<<n<<"\\n";for(int i=0;i<n;i++)std::cout<<rnd.next(-100,-1)<<(i+1==n?"\\n":" ");}\n',
            'validator.cpp': '#include "testlib.h"\nint main(int argc,char** argv){registerValidation(argc,argv);int n=inf.readInt(1,100,"n");inf.readEoln();for(int i=0;i<n;i++){inf.readInt(-100,-1,"value");if(i+1<n)inf.readSpace();}inf.readEoln();inf.readEof();}\n',
            'main.cpp': '#include <iostream>\n#include <algorithm>\nint main(){int n,x,answer=-1000;std::cin>>n;while(n--){std::cin>>x;answer=std::max(answer,x);}std::cout<<answer<<"\\n";}\n',
            'wrong.py': 'n=int(input())\na=list(map(int,input().split()))\nprint(max(0,max(a)))\n',
        }
        for name, content in sources.items():
            source = doc['solutions' if name in doc['solutions'] else 'files'][name]
            source.update(put_blob(db, problem, content.encode()))
        doc['testsets']['tests'].update(tests={}, script='', groups_enabled=True, groups={'small': {'points_policy': 'COMPLETE_GROUP', 'feedback_policy': 'COMPLETE', 'dependencies': []}})
        row.document = copy.deepcopy(doc)
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    state = owner.get(f'/api/problems/{problem}').json()
    requested = owner.post(f'/api/problems/{problem}/jobs', json={'kind': 'stress', 'version': state['version'], 'options': {
        'patterns': [{'name': 'short', 'command': 'gen [4..8] {seed}'}, {'name': 'long', 'command': 'gen [9..12] {seed}'}],
        'candidates': ['wrong.py'], 'iterations': 2, 'stop_on_first': False, 'counterexample_limit': 5,
        'group': 'small', 'minimize': True, 'minimize_seconds': 20,
    }})
    assert requested.status_code == 202, requested.text
    process_job(claim_job())
    job = owner.get(f'/api/problems/{problem}/jobs/{requested.json()["id"]}').json()
    assert job['status'] == 'completed', job
    examples = job['result']['counterexamples']
    assert len(examples) == 2 and [row['attempts'] for row in job['result']['pattern_summaries']] == [1, 1]
    assert all(example['minimized']['input_sha'] != example['input_sha'] for example in examples)
    for example in examples:
        reduced = example['minimized']
        values = list(map(int, read_blob(reduced['input_sha']).split()))
        assert 1 <= values[0] == len(values) - 1 and all(-100 <= value <= -1 for value in values[1:])
        assert read_blob(reduced['answer_sha']).strip() == str(max(values[1:])).encode()
        assert reduced['findings'][0]['verdict'] == 'WA' and read_blob(reduced['findings'][0]['actual_sha']).strip() == b'0'
        assert example['range_values'] and '[' not in example['command']
    assert len(job['result']['coverage']['example_ids']) == 1
    added = owner.post(f'/api/problems/{problem}/jobs/{job["id"]}/counterexamples/apply', json={
        'version': state['version'], 'testset': 'tests', 'group': 'small', 'use_minimized': True, 'counterexample_ids': job['result']['coverage']['example_ids'],
    })
    assert added.status_code == 200, added.text
    test = next(iter(owner.get(f'/api/problems/{problem}').json()['document']['testsets']['tests']['tests'].values()))
    assert test['group'] == 'small' and test['command'] == ''
