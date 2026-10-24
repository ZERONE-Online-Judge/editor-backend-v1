import copy
import threading
from collections import Counter
import pytest
from sqlalchemy import select
from app.config import get_settings
from app.db import Job, PreparationEntry, Problem, SessionLocal, now
from app.demo import populate
from app.pipeline import Pipeline
from app.preparation_cache import expire_entries
from app.sandbox import SandboxError
from app.storage import blob_path, put_blob, read_blob


@pytest.fixture
def document(problem):
    with SessionLocal() as db:
        doc = populate(db, db.get(Problem, problem))
        doc['testsets']['tests']['tests']['3']['command'] = 'gen 3'
        db.commit()
    return doc


@pytest.fixture
def make_pipeline(problem, monkeypatch):
    class FakeSandbox:
        agent_transport = None
        settings = get_settings()
        def __init__(self, check, **kwargs): self.check_cancelled = check
        def resolve_image(self, label): return 'image-digest-v1'
        def close(self): pass
    monkeypatch.setattr('app.pipeline.Sandbox', FakeSandbox)
    monkeypatch.setattr('app.pipeline.require_agent_job', lambda job: None)
    def make(doc, *, kind='verify', options=None, check=lambda: None, problem_id=problem):
        with SessionLocal() as db:
            owner = db.get(Problem, problem_id).owner_id
            job = Job(problem_id=problem_id, user_id=owner, kind=kind, document=copy.deepcopy(doc),
                      document_hash='test', workspace_version=0, revision=0,
                      options={'testset':'tests', 'solutions':['main.cpp'], 'test_indices':[],
                               'snapshot':'working_copy', 'execution_backend':'editor', **(options or {})})
            db.add(job); db.commit(); db.refresh(job)
        p = Pipeline(job, lambda value: None, check)
        calls = Counter()
        lock = threading.Lock()
        def count(stage):
            with lock: calls[stage] += 1
        def generator(command):
            count('input')
            return ('gen ' + command.split()[-1] + '\n').encode()
        def validator(*args):
            count('validator')
            return True, {'stderr':''}
        def solution(name, inp, answer=b'', preparation=False):
            count('answer' if preparation else 'judge')
            return 'AC', {'output':b'42\n', 'cpu_ms':calls['judge'] if not preparation else 999,
                          'wall_ms':1, 'memory_kb':2, 'stderr':''}
        def checker(*args):
            count('checker')
            return 'AC', {'stderr':'', 'score':0}
        p.generator, p.validator, p.run_solution, p.checker = generator, validator, solution, checker
        return p, calls
    return make


def test_repeat_skips_all_preparation_but_rejudges_every_case(document, make_pipeline):
    first, calls = make_pipeline(document)
    first.verify()
    assert calls == {'input':1, 'validator':3, 'answer':3, 'checker':6, 'judge':3}
    second, calls = make_pipeline(document)
    second.verify()
    assert calls == {'checker':3, 'judge':3}
    assert second.result['preparation_cache']['reused'] == {'input':1, 'validator':3, 'answer':3, 'checker':3, 'sample':0}
    assert all(row['main_cpu_ms'] is None for row in second.result['testsets']['tests'])
    assert [c['cpu_ms'] for c in second.result['solutions'][0]['cases']] != [999] * 3
    assert first.result['testsets']['tests'][2]['input_sha'] == second.result['testsets']['tests'][2]['input_sha']


def test_all_sets_reuses_each_preparation_scope_but_rejudges_every_case(document, make_pipeline):
    document['testsets']['samples'] = copy.deepcopy(document['testsets']['tests'])
    single, _ = make_pipeline(document)
    single.verify()
    expanded, calls = make_pipeline(document, options={'all_testsets': True})
    expanded.verify()
    assert calls['judge'] == 6 and calls['validator'] == 3
    assert expanded.result['preparation_cache']['reused']['validator'] == 3
    repeated, calls = make_pipeline(document, options={'all_testsets': True})
    repeated.verify()
    assert calls == {'judge': 6, 'checker': 6}
    assert not any(repeated.result['preparation_cache']['executed'].values())
    assert repeated.result['preparation_cache']['reused']['validator'] == 6
    assert repeated.result['verified']
    assert repeated.result['verification_progress']['total'] == 6
    assert {(case['testset'], case['index']) for case in repeated.result['solutions'][0]['cases']} == {
        (name, index) for name in ['tests', 'samples'] for index in [1, 2, 3]}


@pytest.mark.parametrize('change,expected', [
    ('prose', {}), ('wrong_solution', {}), ('validator', {'validator':3}),
    ('main', {'answer':3}), ('checker', {'checker':3}),
    ('generator', {'input':1}), ('generator_args', {'input':1, 'validator':1, 'answer':1, 'checker':1}),
    ('input', {'validator':1, 'answer':1, 'checker':1}), ('group', {'validator':1}),
    ('limits', {'answer':3}), ('aux', {}),
    ('resource', {'input':1, 'validator':3, 'answer':3, 'checker':3}),
])
def test_only_affected_dependencies_execute(problem, document, make_pipeline, change, expected):
    first, _ = make_pipeline(document); first.prepare()
    def store(content):
        with SessionLocal() as db:
            value = put_blob(db, problem, content)['sha256']; db.commit(); return value
    if change == 'prose': document['statements']['korean']['legend'] += 'Updated prose'
    if change == 'wrong_solution': document['solutions']['wrong.py']['sha256'] = store(b'new code')
    if change == 'validator': document['files']['validator.cpp']['sha256'] = store(b'changed validator')
    if change == 'main': document['solutions']['main.cpp']['sha256'] = store(b'changed main')
    if change == 'checker':
        document['files']['checker.cpp'] = {**document['files']['validator.cpp'], 'name':'checker.cpp', 'sha256':store(b'checker')}
        document['checker'] = 'checker.cpp'
    if change == 'generator': document['files']['gen.cpp']['sha256'] = store(b'changed generator')
    if change == 'generator_args': document['testsets']['tests']['tests']['3']['command'] = 'gen 4'
    if change == 'input': document['testsets']['tests']['tests']['2']['input_sha'] = store(b'new input\n')
    if change == 'group': document['testsets']['tests']['tests']['2']['group'] = 'small'
    if change == 'limits': document['info']['time_limit_ms'] += 100
    if change in {'resource','aux'}:
        document['files']['header.h'] = {'name':'header.h', 'kind':change, 'language':'text',
                                       'sha256':store(b'header'), 'attach_to_build':change != 'aux'}
    second, _ = make_pipeline(document); second.prepare()
    actual = {key:value for key,value in second.result['preparation_cache']['executed'].items() if value}
    assert actual == expected


@pytest.mark.parametrize('kind,options', [('prepare',{}), ('package',{}), ('verify',{'force_prepare':True})])
def test_explicit_generation_package_and_force_always_execute(document, make_pipeline, kind, options):
    first, _ = make_pipeline(document); first.prepare()
    second, calls = make_pipeline(document, kind=kind, options=options); second.prepare()
    assert calls == {'input':1, 'validator':3, 'answer':3, 'checker':3}
    assert not any(second.result['preparation_cache']['reused'].values())


def test_successful_explicit_preparation_seeds_later_verification(document, make_pipeline):
    first, _ = make_pipeline(document, kind='prepare'); first.prepare()
    second, calls = make_pipeline(document); second.prepare()
    assert not calls
    assert sum(second.result['preparation_cache']['reused'].values()) == 10


def test_failed_validation_and_failed_main_are_never_cached(document, make_pipeline):
    p, _ = make_pipeline(document, options={'test_indices':[1]})
    p.validator = lambda *args: (False, {'stderr':'invalid'})
    with pytest.raises(SandboxError, match='입력 조건 위반'): p.prepare()
    second, calls = make_pipeline(document, options={'test_indices':[1]})
    second.run_solution = lambda *args, **kwargs: ('TLE', {'stderr':'timeout'})
    with pytest.raises(SandboxError, match='TLE'): second.prepare()
    third, calls = make_pipeline(document, options={'test_indices':[1]}); third.prepare()
    assert calls == {'answer':1, 'checker':1}


def test_missing_or_corrupt_output_is_rebuilt(document, make_pipeline):
    first, _ = make_pipeline(document); first.prepare()
    answer = first.result['testsets']['tests'][0]['answer_sha']
    blob_path(answer).write_bytes(b'corrupt')
    second, calls = make_pipeline(document); second.prepare()
    assert calls['answer'] >= 1
    assert read_blob(answer) == b'42\n'
    generated = second.result['testsets']['tests'][2]['input_sha']
    blob_path(generated).unlink()
    third, calls = make_pipeline(document); third.prepare()
    assert calls['input'] == 1


def test_runtime_revision_and_expiry_invalidate_entries(document, make_pipeline, monkeypatch):
    first, _ = make_pipeline(document); first.prepare()
    monkeypatch.setattr(get_settings(), 'runtime_revision', 'changed-runtime')
    second, calls = make_pipeline(document); second.prepare()
    assert calls == {'input':1, 'validator':3, 'answer':3, 'checker':3}
    with SessionLocal() as db:
        for row in db.scalars(select(PreparationEntry)): row.expires_at = now() - 1
        db.commit(); expire_entries(db); db.commit()
        assert not db.scalars(select(PreparationEntry)).all()
    third, calls = make_pipeline(document); third.prepare()
    assert calls == {'input':1, 'validator':3, 'answer':3, 'checker':3}


def test_cache_never_crosses_problem_boundary(owner, problem, document, make_pipeline):
    first, _ = make_pipeline(document); first.prepare()
    other = owner.post('/api/problems', json={'name':'Other','slug':'other'}).json()['id']
    with SessionLocal() as db:
        from app.db import Blob
        for row in list(db.scalars(select(Blob).where(Blob.problem_id == problem))):
            put_blob(db, other, read_blob(row.sha256))
        db.commit()
    second, calls = make_pipeline(document, problem_id=other); second.prepare()
    assert calls == {'input':1, 'validator':3, 'answer':3, 'checker':3}


def test_cancellation_is_checked_before_serving_cached_results(document, make_pipeline):
    first, _ = make_pipeline(document); first.prepare()
    def cancelled(): raise RuntimeError('cancelled')
    second, _ = make_pipeline(document, check=cancelled)
    with pytest.raises(RuntimeError, match='cancelled'): second.prepare()


def test_parallel_jobs_can_publish_the_same_successful_stage(document, make_pipeline):
    from concurrent.futures import ThreadPoolExecutor
    jobs = [make_pipeline(document)[0] for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda p:p.prepare(), jobs))
    assert [row['input_sha'] for row in jobs[0].result['testsets']['tests']] == [row['input_sha'] for row in jobs[1].result['testsets']['tests']]
    later, calls = make_pipeline(document); later.prepare()
    assert not calls


def test_distinct_generator_test_positions_do_not_collapse_unseeded_inputs(document, make_pipeline):
    document['info']['skip_duplicate_validation'] = True
    tests = document['testsets']['tests']['tests']
    tests['2']['command'] = tests['3']['command']
    first, calls = make_pipeline(document); first.prepare()
    assert calls['input'] == 2
    second, calls = make_pipeline(document); second.prepare()
    assert not calls
    assert second.result['preparation_cache']['reused']['input'] == 2


def test_resource_compile_only_policy_changes_runtime_inputs(problem, document, make_pipeline):
    with SessionLocal() as db:
        sha = put_blob(db, problem, b'header')['sha256']; db.commit()
    document['files']['header.h'] = {'name':'header.h', 'kind':'resource', 'language':'text', 'sha256':sha}
    first, _ = make_pipeline(document); first.prepare()
    document['files']['header.h']['resource_policy'] = {'languages':['cpp17'], 'targets':['SOLUTION'], 'stages':['COMPILE']}
    second, calls = make_pipeline(document); second.prepare()
    assert calls['answer'] == 3


def test_committed_verification_reuses_checks_but_never_regenerates_stored_data(document, make_pipeline):
    first, _ = make_pipeline(document, options={'snapshot':'committed'}); first.prepare()
    second, calls = make_pipeline(document, options={'snapshot':'committed'}); second.prepare()
    assert not calls
    assert second.result['preparation_cache']['reused']['answer'] == 0
    forced, calls = make_pipeline(document, options={'snapshot':'committed', 'force_prepare':True}); forced.prepare()
    assert calls == {'validator':3, 'checker':3}
    assert [row['answer_sha'] for row in forced.result['testsets']['tests']] == [test['answer_sha'] for test in document['testsets']['tests']['tests'].values()]


def test_display_sample_changes_recheck_sample_only(document, make_pipeline):
    test = document['testsets']['tests']['tests']['1']
    test.update(statement_input='2 3\n', statement_output='42\n', verify_statement_io=True)
    first, _ = make_pipeline(document); first.prepare()
    second, calls = make_pipeline(document); second.prepare()
    assert not calls
    test['statement_output'] = '42 \n'
    third, calls = make_pipeline(document); third.prepare()
    assert calls == {'validator':1, 'answer':1, 'checker':1}
    assert third.result['preparation_cache']['executed']['sample'] == 1


def test_force_prepare_option_is_only_available_for_verification(owner, problem):
    response = owner.post(f'/api/problems/{problem}/jobs', json={
        'kind':'prepare', 'version':0, 'options':{'force_prepare':True}})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'invalid_options'
