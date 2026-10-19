"""Opt-in physical verification of language limits and ZOJ stress judging."""
import copy
import json
import time
from uuid import uuid4

from sqlalchemy import delete, select

from .db import Access, Blob, Job, Problem, Revision, SessionLocal, User, WorkingCopy, now
from .demo import populate
from .jobs import JobRequest, _create_job
from .schema import Document
from .storage import blob_path, put_blob


def wait(job_id):
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            state, result = job.status, copy.deepcopy(job.result)
        if state not in {'queued', 'running'}:
            if state != 'completed':
                raise RuntimeError('Physical language check failed: ' + result.get('error', state))
            return result
        time.sleep(2)
    raise RuntimeError('Physical language check timed out.')


def cases(rows):
    for row in rows:
        for case in row['cases']:
            if case.get('cpu_ms') is None or case.get('memory_kb') is None or not case.get('actual_sha'):
                raise RuntimeError('Actual judge metrics or stdout are missing: ' + row['name'])


def run():
    fixture_id = uuid4().hex
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.central_account_id.is_not(None)).order_by(User.created_at).limit(1))
            if not user:
                raise RuntimeError('An existing ZOJ account must sign into Editor first.')
            problem = Problem(id=fixture_id, owner_id=user.id, slug='language-check-' + fixture_id, document={})
            db.add(problem)
            db.flush()
            doc = populate(db, problem)
            doc['info'].update(name='Temporary language policy check', time_limit_ms=300, memory_limit_mb=128)
            doc['solutions'] = {'main.cpp': doc['solutions']['main.cpp']}
            doc['testsets']['tests']['tests'] = {'1': doc['testsets']['tests']['tests']['1']}
            doc['language_resource_limits'] = {
                'pypy3': {'disable_auto_adjustment': True},
                'kotlin': {'disable_auto_adjustment': True, 'time_limit_ms': 800, 'memory_limit_mb': 384},
            }
            allocate = 'a,b=map(int,input().split())\nx=bytearray(200*1024*1024)\nfor i in range(0,len(x),4096):\n    x[i]=1\nprint(a+b)\n'
            sources = {
                'python-auto.py': ('python3', 'TL', 'while True:\n    pass\n'),
                'pypy-disabled.py': ('pypy3', 'TL', 'while True:\n    pass\n'),
                'python-memory.py': ('python3', 'OK', allocate),
                'pypy-memory.py': ('pypy3', 'ML', allocate),
                'kotlin-override.kt': ('kotlin', 'TL', 'fun main() { var x = 0L; while (true) { x++; if (x == -1L) println(x) } }\n'),
            }
            for name, (language, tag, code) in sources.items():
                doc['solutions'][name] = {'name': name, 'kind': 'source', 'language': language, 'tag': tag, 'extra_tags': {}, **put_blob(db, fixture_id, code.encode())}
            doc = Document.model_validate(doc).model_dump()
            problem.document = doc
            db.add(Access(problem_id=fixture_id, user_id=user.id, role='OWNER'))
            db.add(Revision(problem_id=fixture_id, number=0, author_id=user.id, message='Temporary policy check', document=copy.deepcopy(doc)))
            db.add(WorkingCopy(problem_id=fixture_id, user_id=user.id, base_revision=0, base_document=copy.deepcopy(doc), document=copy.deepcopy(doc)))
            db.commit()
            verification = _create_job(fixture_id, JobRequest(kind='verify', version=0, reuse=False), user, db)
        result = wait(verification['id'])
        if result['runtime']['backend'] != 'zoj' or not result['verified']:
            raise RuntimeError('The ZOJ policy verification did not match expectations.')
        cases(result['solutions'])
        bounds = {'python-auto.py': (2500, 4000), 'pypy-disabled.py': (200, 1500), 'kotlin-override.kt': (600, 1800)}
        for solution in result['solutions']:
            if not solution['complete'] or len(solution['cases']) != 1:
                raise RuntimeError('Missing complete physical results: ' + solution['name'])
            if any(not record.get('node_name') or not record.get('agent_version') for record in solution['judge_runs']):
                raise RuntimeError('Missing physical judge identity.')
            value = solution['cases'][0]['cpu_ms']
            if solution['name'] in bounds and not bounds[solution['name']][0] <= value <= bounds[solution['name']][1]:
                raise RuntimeError('Applied CPU allowance differs from the selected policy: ' + solution['name'])
            print(json.dumps({'stage': 'verification', 'source': solution['name'], 'cases': solution['cases'], 'runs': solution['judge_runs']}, ensure_ascii=False), flush=True)
        with SessionLocal() as db:
            user = db.get(User, user.id)
            stress = _create_job(fixture_id, JobRequest(kind='stress', version=0, reuse=False, options={
                'generator': 'gen {seed}', 'reference': 'main.cpp',
                'candidates': ['python-auto.py', 'pypy-disabled.py', 'kotlin-override.kt'],
                'iterations': 2, 'stop_mode': 'target', 'counterexample_limit': 5,
            }), user, db)
        result = wait(stress['id'])
        if result['runtime']['backend'] != 'zoj' or result['iterations'] != 2:
            raise RuntimeError('Stress did not finish both iterations through ZOJ.')
        cases(result['stress_results'])
        if len(result['stress_results']) != 4 or any(len(row['cases']) != 2 for row in result['stress_results']):
            raise RuntimeError('Stress omitted per-solution physical results.')
        runs = result.get('stress_judge_runs', [])
        if len(runs) != 8 or any(not row.get('node_name') or not row.get('agent_version') for row in runs):
            raise RuntimeError('Stress did not record every actual ZOJ execution.')
        print(json.dumps({'stage': 'stress', 'summary': result['stress_result_summary'], 'runs': runs}, ensure_ascii=False), flush=True)
        print('Physical language policy and stress checks passed; removing temporary fixture.', flush=True)
    finally:
        with SessionLocal() as db:
            for job in db.scalars(select(Job).where(Job.problem_id == fixture_id, Job.status.in_(['queued', 'running']))):
                job.status, job.finished_at = 'cancelled', now()
            db.commit()
            hashes = db.scalars(select(Blob.sha256).where(Blob.problem_id == fixture_id)).all()
            for model in (Job, WorkingCopy, Revision, Access, Blob):
                db.execute(delete(model).where(model.problem_id == fixture_id))
            db.execute(delete(Problem).where(Problem.id == fixture_id))
            db.commit()
            for sha in hashes:
                if not db.scalar(select(Blob.sha256).where(Blob.sha256 == sha).limit(1)):
                    blob_path(sha).unlink(missing_ok=True)
