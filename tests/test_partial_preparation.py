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
