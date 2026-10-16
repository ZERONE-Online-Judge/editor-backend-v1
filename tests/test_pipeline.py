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


def test_real_all_testsets_preserves_scope_and_reuses_preparation(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        sample = copy.deepcopy(doc['testsets']['tests'])
        sample['tests'] = {'1': {**sample['tests']['1'],
            'input_sha': put_blob(db, problem, b'1 0\n')['sha256'], 'answer_sha': None}}
        doc['testsets']['samples'] = sample
        doc['solutions']['wrong.py']['extra_tags'] = {'samples': 'OK'}
        row.document = doc
        db.commit()
    state = owner.get(f'/api/problems/{problem}').json()
    def execute():
        request = owner.post(f'/api/problems/{problem}/jobs', json={
            'kind': 'verify', 'version': state['version'], 'reuse': False,
            'options': {'execution_backend': 'editor', 'all_testsets': True}})
        assert request.status_code == 202, request.text
        process_job(claim_job())
        job = owner.get(f'/api/problems/{problem}/jobs/{request.json()["id"]}').json()
        assert job['status'] == 'completed', job
        assert job['result']['verified'], job
        return job['result']
    cold, warm = execute(), execute()
    assert cold['processed_testsets'] == warm['processed_testsets'] == ['tests', 'samples']
    assert warm['verification_progress']['current'] == warm['verification_progress']['total'] == 8
    assert warm['verification_progress']['percent'] == 100
    assert not any(warm['preparation_cache']['executed'].values())
    wrong = next(s for s in warm['solutions'] if s['name'] == 'wrong.py')
    assert [(s['testset'], s['tag'], s['matched']) for s in wrong['expectations']] == [
        ('tests', 'WA', True), ('samples', 'OK', True)]
    assert [(c['testset'], c['index'], c['verdict']) for c in wrong['cases']] == [
        ('samples', 1, 'AC'), ('tests', 1, 'WA'), ('tests', 2, 'WA'), ('tests', 3, 'WA')]
    assert all(c['cpu_ms'] is not None and c['memory_kb'] is not None for c in wrong['cases'])


def test_real_prepared_inputs_are_reused_but_solutions_are_measured_again(owner, problem, monkeypatch):
    import json
    from collections import Counter
    from pathlib import Path
    from app.sandbox import Sandbox
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        row.document = populate(db, row)
        row.document['testsets']['tests']['tests']['3']['command'] = 'gen 20261016'
        db.get(Revision, (problem, 0)).document = copy.deepcopy(row.document)
        db.commit()
    original = Sandbox.run
    phase = {'name':'cold'}
    calls = {'cold':Counter(), 'warm':Counter()}
    def measured(self, program, *args, **kwargs):
        calls[phase['name']][program.name] += 1
        return original(self, program, *args, **kwargs)
    monkeypatch.setattr(Sandbox, 'run', measured)
    state = owner.get(f'/api/problems/{problem}').json()
    def execute(kind):
        request = owner.post(f'/api/problems/{problem}/jobs', json={
            'kind':kind, 'version':state['version'], 'reuse':False, 'options':{'execution_backend':'editor'}})
        assert request.status_code == 202, request.text
        process_job(claim_job())
        result = owner.get(f'/api/problems/{problem}/jobs/{request.json()["id"]}').json()
        assert result['status'] == 'completed', result
        return result
    started = time.monotonic()
    prepared = execute('verify')
    first_seconds = time.monotonic() - started
    phase['name'] = 'warm'
    started = time.monotonic()
    verified = execute('verify')
    second_seconds = time.monotonic() - started
    assert calls['cold']['gen.cpp'] == 1
    assert calls['cold']['validator.cpp'] == 3
    assert calls['cold']['main.cpp'] == 6
    assert calls['warm']['gen.cpp'] == calls['warm']['validator.cpp'] == 0
    assert calls['warm']['main.cpp'] == calls['warm']['wrong.py'] == 3
    cache = verified['result']['preparation_cache']
    assert cache['reused'] == {'input':1, 'validator':3, 'answer':3, 'checker':3, 'sample':0}
    assert not any(cache['executed'].values())
    assert verified['result']['verified']
    for solution in verified['result']['solutions']:
        assert len(solution['cases']) == 3
        assert all(case['cpu_ms'] is not None and case['memory_kb'] is not None for case in solution['cases'])
    for old, new in zip(prepared['result']['testsets']['tests'], verified['result']['testsets']['tests']):
        assert (old['input_sha'], old['answer_sha']) == (new['input_sha'], new['answer_sha'])
        assert new['main_cpu_ms'] is None
    if os.environ.get('EDITOR_CACHE_BENCHMARK_PATH'):
        Path(os.environ['EDITOR_CACHE_BENCHMARK_PATH']).write_text(json.dumps({
            'cold_verification_seconds':first_seconds, 'warm_verification_seconds':second_seconds,
            'sandbox_runs':calls, 'preparation_cache':cache,
            'solutions':[{key:solution[key] for key in ('name','matched','cases')} for solution in verified['result']['solutions']],
        }, ensure_ascii=False, indent=2))


def test_real_selected_input_and_answer_regeneration_preserves_other_tests(owner,problem):
    with SessionLocal() as db:
        row=db.get(Problem,problem)
        doc=populate(db,row)
        doc['testsets']['tests']['tests']['3']['command']='gen 20261015'
        original=copy.deepcopy(doc['testsets']['tests']['tests'])
        row.document=doc;db.get(Revision,(problem,0)).document=copy.deepcopy(doc);db.commit()
    state=owner.get(f'/api/problems/{problem}').json()
    def execute(mode,version):
        response=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'prepare','version':version,'options':{'test_indices':[3],'prepare_mode':mode,'auto_apply':True}})
        assert response.status_code==202,response.text
        process_job(claim_job())
        job=owner.get(f'/api/problems/{problem}/jobs/{response.json()["id"]}').json()
        assert job['status']=='completed',job
        assert len(job['result']['testsets']['tests'])==1
        return job,owner.get(f'/api/problems/{problem}').json()
    inputs,state=execute('inputs',state['version'])
    tests=state['document']['testsets']['tests']['tests']
    assert tests['1']==original['1'] and tests['2']==original['2']
    assert tests['3']['input_sha']!=original['3']['input_sha'] and tests['3']['answer_sha'] is None
    assert not any('main.cpp' in event['label'] for event in inputs['result']['events'])
    generated=tests['3']['input_sha']
    answers,state=execute('answers',state['version'])
    tests=state['document']['testsets']['tests']['tests']
    assert tests['3']['input_sha']==generated and tests['3']['answer_sha']
    assert tests['1']==original['1'] and tests['2']==original['2']
    assert not any('컴파일 · gen.cpp' in event['label'] for event in answers['result']['events'])


def test_real_display_samples_are_checked_without_replacing_judge_data(owner, problem):
    with SessionLocal() as db:
        row=db.get(Problem,problem)
        doc=populate(db,row)
        original=copy.deepcopy(doc['testsets']['tests']['tests']['1'])
        doc['testsets']['tests']['tests']['1'].update(statement_input='2 3\n',statement_output='5 \n\n',verify_statement_io=True)
        row.document=doc
        db.get(Revision,(problem,0)).document=copy.deepcopy(doc)
        db.commit()
    state=owner.get(f'/api/problems/{problem}').json()
    request=owner.post(f'/api/problems/{problem}/jobs',json={'kind':'prepare','version':state['version']})
    assert request.status_code==202,request.text
    process_job(claim_job())
    job=owner.get(f'/api/problems/{problem}/jobs/{request.json()["id"]}').json()
    assert job['status']=='completed',job
    sample=job['result']['testsets']['tests'][0]
    assert sample['statement_io_validation']=='VALID'
    assert sample['input_sha']==original['input_sha'] and sample['answer_sha']==original['answer_sha']
    assert sample['statement_output']=='5 \n\n'


def test_real_solution_expected_tle_only_in_large_group(owner, problem):
    with SessionLocal() as db:
        row = db.get(Problem, problem)
        doc = populate(db, row)
        doc['info']['time_limit_ms'] = 250
        group = {'points_policy': 'COMPLETE_GROUP', 'feedback_policy': 'COMPLETE', 'dependencies': []}
        doc['testsets']['tests'].update(groups_enabled=True, groups={'small': group, 'large': group})
        for test in doc['testsets']['tests']['tests'].values():
            test['group'] = 'large' if test['index'] == 3 else 'small'
        code = b'#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;if(a>1000){for(;;){asm volatile("" ::: "memory");}}std::cout<<a+b<<"\\n";}\n'
        doc['solutions']['small.cpp'] = {'name':'small.cpp','kind':'source','language':'cpp17',**put_blob(db,problem,code),
            'tag':'TL','extra_tags':{},'group_tags':{'tests':{'small':'OK','large':'TL'}}}
        row.document = doc
        db.get(Revision, (problem, 0)).document = copy.deepcopy(doc)
        db.commit()
    state = owner.get(f'/api/problems/{problem}').json()
    request = owner.post(f'/api/problems/{problem}/jobs', json={'kind':'verify','version':state['version'], 'options':{'solutions':['small.cpp']}})
    assert request.status_code == 202, request.text
    process_job(claim_job())
    job = owner.get(f'/api/problems/{problem}/jobs/{request.json()["id"]}').json()
    assert job['status'] == 'completed', job
    result = job['result']['solutions'][0]
    assert result['matched'] and result['expectations_complete']
    assert [case['verdict'] for case in result['cases']] == ['AC','AC','TLE']
    assert [(scope['group'],scope['tag'],scope['matched']) for scope in result['expectations']] == [('small','OK',True),('large','TL',True)]


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
    state["document"]["common_statement"] = "공통 문제 설명 $a+b$"
    state["document"]["common_tutorial"] = "공통 비공개 해설"
    state["document"]["testsets"]["tests"]["script"] = "repeat seed in 10..12\n gen {seed} > $\nend"
    state["document"]["solutions"]["wrong.py"]["extra_tags"] = {"tests": "WA"}
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
    assert doc['common_statement'] == '공통 문제 설명 $a+b$'
    assert doc['common_tutorial'] == '공통 비공개 해설'
    assert doc['solutions']['wrong.py']['extra_tags'] == {'tests': 'WA'}
    assert [t['command'] for t in doc['testsets']['tests']['tests'].values() if t['command']] == ['gen 10', 'gen 11', 'gen 12']


def test_real_stress_shares_inputs_and_collects_multiple_solution_failures(owner, problem):
    prepare_problem(problem)
    state = owner.get(f"/api/problems/{problem}").json()
    duplicate = copy.deepcopy(state["document"]["solutions"]["wrong.py"])
    duplicate["name"] = "wrong2.py"
    state["document"]["solutions"]["wrong2.py"] = duplicate
    passing = {**state["document"]["solutions"]["main.cpp"], "name": "passing.cpp", "tag": "OK"}
    state["document"]["solutions"]["passing.cpp"] = passing
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]})
    requested = owner.post(f"/api/problems/{problem}/jobs", json={"kind": "stress", "version": saved.json()["version"], "options": {
        "generator": "gen {seed}", "reference": "main.cpp", "candidates": ["wrong.py", "wrong2.py", "passing.cpp"],
        "iterations": 3, "stop_on_first": False, "counterexample_limit": 5,
    }})
    assert requested.status_code == 202, requested.text
    process_job(claim_job())
    job = owner.get(f"/api/problems/{problem}/jobs/{requested.json()['id']}").json()
    assert job["status"] == "completed", job
    assert len(job["result"]["counterexamples"]) > 1
    assert all([finding["verdict"] for finding in ce["findings"]] == ["WA", "WA"] for ce in job["result"]["counterexamples"])
    assert all(row["tested"] == 3 for row in job["result"]["candidate_summaries"])
    from app.storage import read_blob
    rows = {row["name"]: row for row in job["result"]["stress_results"]}
    assert len(rows) == 4 and all(len(row["cases"]) == 3 for row in rows.values())
    for row in rows.values():
        assert row["max_cpu_ms"] == max(case["cpu_ms"] for case in row["cases"])
        assert row["max_memory_kb"] == max(case["memory_kb"] for case in row["cases"])
        assert all(case["wall_ms"] is not None and case["actual_sha"] for case in row["cases"])
    assert all(case["verdict"] == "AC" and read_blob(case["actual_sha"]) == read_blob(case["answer_sha"])
               for case in rows["passing.cpp"]["cases"])


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
