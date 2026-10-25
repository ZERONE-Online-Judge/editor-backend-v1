import copy
import pytest
from app.db import SessionLocal
from app.storage import put_blob
from test_invocation_scope import scoped_pipeline


def pipeline(problem,mode):
    p,_=scoped_pipeline(problem,'prepare',prepare_mode=mode,test_indices=[2])
    with SessionLocal() as db:
        def blob(content): return put_blob(db,problem,content)['sha256']
        original,answer=blob(b'old input\n'),blob(b'old answer\n')
        db.commit()
    p.doc['testsets']['tests']['tests']['2']={**p.doc['testsets']['tests']['tests']['1'],'index':2,'command':'gen 2','input_sha':original,'answer_sha':answer}
    def store(content):
        with SessionLocal() as db:
            sha=put_blob(db,problem,content)['sha256'];db.commit();return sha
    p.blob=store
    return p,original,answer


def unexpected(*args,**kwargs): raise AssertionError('This execution step was not requested')


def test_input_only_regeneration_invalidates_answer_and_never_executes_main(problem):
    p,original,answer=pipeline(problem,'inputs')
    p.main=unexpected;p.run_solution=unexpected;p.checker=unexpected
    p.generator=lambda command:b'new input\n'
    p.prepare()
    rows=p.result['testsets']['tests']
    assert len(rows)==1 and rows[0]['index']==2
    assert rows[0]['input_sha']!=original and rows[0]['answer_sha'] is None
    assert p.doc['testsets']['tests']['tests']['2']['answer_sha']==answer


def test_validator_only_preserves_stored_bytes_without_main_or_generator(problem):
    p,original,answer=pipeline(problem,'validate')
    p.main=unexpected;p.run_solution=unexpected;p.checker=unexpected;p.generator=unexpected
    p.prepare()
    row=p.result['testsets']['tests'][0]
    assert row['input_sha']==original and row['answer_sha']==answer
    assert row['main_cpu_ms'] is None


def test_answer_only_uses_stored_input_and_never_regenerates_it(problem):
    p,original,answer=pipeline(problem,'answers')
    p.generator=unexpected
    p.prepare()
    row=p.result['testsets']['tests'][0]
    assert row['input_sha']==original and row['answer_sha']!=answer


@pytest.mark.parametrize('all_sets', [False, True])
def test_whole_script_input_check_covers_stored_and_generated_inputs_without_main(problem, all_sets):
    p,seen=scoped_pipeline(problem,'prepare',prepare_mode='inputs',all_testsets=all_sets)
    p.doc['testsets']['tests']['script']='repeat seed in 2..3\n gen {seed} > $\nend'
    p.doc['solutions']={}
    p.main=unexpected;p.run_solution=unexpected;p.checker=unexpected
    generated=[]
    p.generator=lambda command: (generated.append(command) or (command+'\n').encode())
    before=copy.deepcopy(p.doc)
    p.prepare()
    assert generated==['gen 2','gen 3']
    assert seen==['tests']*3 + (['samples','subtask1'] if all_sets else [])
    assert p.result['processed_testsets']==(['tests','samples','subtask1'] if all_sets else ['tests'])
    assert [row['index'] for row in p.result['testsets']['tests']]==[1,2,3]
    assert all(row['validator']=='VALID' and row['main_cpu_ms'] is None
               for rows in p.result['testsets'].values() for row in rows)
    assert p.doc==before


def scripted_copy(owner, problem):
    from test_invocation_options import ready
    state=ready(owner,problem)
    state['document']['solutions']={}
    state['document']['testsets']['tests']['script']='gen 4 > 4'
    state['document']['testsets']['other']=copy.deepcopy(state['document']['testsets']['tests'])
    response=owner.put(f'/api/problems/{problem}/working-copy',json={
        'version':state['version'],'document':state['document']})
    assert response.status_code==200,response.text
    return response.json()['version']


@pytest.mark.parametrize('all_sets', [False, True])
def test_whole_script_input_check_is_admitted_without_main_and_uses_only_agents(owner,problem,all_sets):
    version=scripted_copy(owner,problem)
    response=owner.post(f'/api/problems/{problem}/jobs',json={
        'kind':'prepare','version':version,'options':{'prepare_mode':'inputs','all_testsets':all_sets,'check_only':True}})
    assert response.status_code==202,response.text
    options=response.json()['options']
    assert options['execution_backend']=='zoj' and options['execution_transport']=='agent'
    assert options['all_testsets'] is all_sets and options['test_indices']==[]
    assert options['check_only'] is True
    assert options['auto_apply'] is False and 'reference_output_protocol' not in options
    state=owner.get(f'/api/problems/{problem}').json()
    assert state['version']==version and state['document']['testsets']['tests']['script']=='gen 4 > 4'


@pytest.mark.parametrize('options', [
    {'prepare_mode':'inputs','test_indices':[1]},
    {'prepare_mode':'validate'}, {'prepare_mode':'answers'},
])
def test_script_still_requires_materialization_for_partial_or_stored_only_work(owner,problem,options):
    version=scripted_copy(owner,problem)
    response=owner.post(f'/api/problems/{problem}/jobs',json={
        'kind':'prepare','version':version,'options':options})
    assert response.status_code==400,response.text
    assert response.json()['error']['code']=='materialize_first'


def test_empty_input_check_cannot_claim_success(owner,problem):
    response=owner.post(f'/api/problems/{problem}/jobs',json={
        'kind':'prepare','version':0,'options':{'prepare_mode':'inputs','all_testsets':True}})
    assert response.status_code==400,response.text
    assert response.json()['error']['code']=='empty_scope'
