"""Explicit temporary-fixture check of the deployed worker and physical agents."""
import argparse
import copy
import json
import time
from uuid import uuid4

from sqlalchemy import delete, select

from .config import get_settings
from .db import Access, Blob, Job, Problem, Revision, SessionLocal, User, WorkingCopy, now
from .demo import populate
from .jobs import JobRequest, _create_job
from .schema import Document
from .storage import blob_path, put_blob


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--temporary-fixture', action='store_true', required=True)
    parser.parse_args()
    if get_settings().judge_backend != 'zoj':
        raise SystemExit('Configure ZOJ judging before running this check.')
    fixture_id = uuid4().hex
    job_id = None
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.central_account_id.is_not(None)).order_by(User.created_at).limit(1))
            if not user:
                raise SystemExit('Sign into Editor with an existing shared account first.')
            problem = Problem(id=fixture_id, owner_id=user.id, slug='judge-check-' + fixture_id, document={})
            db.add(problem)
            db.flush()
            doc = populate(db, problem)
            doc['info'].update(name='Temporary judge integration check', time_limit_ms=500, memory_limit_mb=32)
            extra = {
                'main.c': ('c99', 'OK', '#include <stdio.h>\nint main(){long long a,b;scanf("%lld%lld",&a,&b);printf("%lld\\n",a+b);}\n'),
                'main.py': ('python3', 'OK', 'a,b=map(int,input().split())\nprint(a+b)\n'),
                'Main.java': ('java8', 'OK', 'import java.util.Scanner; public class Main { public static void main(String[] a){ Scanner s=new Scanner(System.in);System.out.println(s.nextLong()+s.nextLong());}}\n'),
                'tle.cpp': ('cpp17', 'TL', 'int main(){volatile unsigned long long x=0;while(true){++x;}}\n'),
                'memory.cpp': ('cpp17', 'ML', '#include <vector>\n#include <cstdlib>\n#include <cstring>\nint main(){std::vector<void*> p;while(true){void* q=malloc(65536);if(!q)return 1;memset(q,123,65536);p.push_back(q);}}\n'),
                'broken.cpp': ('cpp17', 'RJ', 'int main( this is not C++\n'),
            }
            for name, (language, tag, code) in extra.items():
                doc['solutions'][name] = {'name': name, 'kind': 'source', 'language': language, 'tag': tag, 'extra_tags': {}, **put_blob(db, fixture_id, code.encode())}
            doc = Document.model_validate(doc).model_dump()
            problem.document = doc
            db.add(Access(problem_id=fixture_id, user_id=user.id, role='OWNER'))
            db.add(Revision(problem_id=fixture_id, number=0, author_id=user.id, message='Temporary deployment check', document=copy.deepcopy(doc)))
            db.add(WorkingCopy(problem_id=fixture_id, user_id=user.id, base_revision=0, base_document=copy.deepcopy(doc), document=copy.deepcopy(doc)))
            db.commit()
            created = _create_job(fixture_id, JobRequest(kind='verify', version=0, reuse=False, options={'execution_backend': 'zoj'}), user, db)
            job_id = created['id']
        deadline = time.monotonic() + 420
        previous = None
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                state = (job.status, job.result.get('summary', ''))
                result = copy.deepcopy(job.result)
            if state != previous:
                print(json.dumps({'stage': state[0], 'summary': state[1]}, ensure_ascii=False), flush=True)
                previous = state
            if state[0] not in {'queued', 'running'}:
                break
            time.sleep(2)
        else:
            raise RuntimeError('The temporary worker check timed out.')
        if state[0] != 'completed':
            raise RuntimeError('The deployed worker did not complete the fixture: ' + result.get('error', state[0]))
        expected = {'main.cpp': 'accepted', 'main.c': 'accepted', 'main.py': 'accepted', 'Main.java': 'accepted',
                    'wrong.py': 'wrong_answer', 'tle.cpp': 'time_limit_exceeded', 'memory.cpp': 'memory_limit_exceeded', 'broken.cpp': 'compile_error'}
        seen = set()
        for solution in result['solutions']:
            name = solution['name']
            records = solution.get('judge_runs', [])
            if not records or any(r['status'] != expected[name] or not r.get('node_name') or not r.get('agent_version') for r in records):
                raise RuntimeError('Unexpected physical judge result: ' + name + ' / ' + json.dumps(records, ensure_ascii=False))
            if solution['matched'] != (name != 'broken.cpp'):
                raise RuntimeError('Expected-tag matching was incorrect: ' + name)
            seen.add(name)
            print(json.dumps({'source': name, 'verdict': expected[name], 'complete': solution['complete'],
                              'runs': [{k: r.get(k) for k in ('node_name', 'agent_version', 'testcase_count', 'runtime_ms', 'memory_kb')} for r in records]}, ensure_ascii=False), flush=True)
        if seen != set(expected) or result.get('mismatches') != ['broken.cpp'] or result['runtime']['backend'] != 'zoj' or result['preparation_runtime']['backend'] != 'editor':
            raise RuntimeError('The complete mixed-language verification result was inconsistent.')
        print('Physical judge integration verified; temporary Editor fixture will be removed.', flush=True)
    finally:
        # Delete only this randomly identified fixture. Never seed accounts,
        # modify a contest, or unlink blobs still owned by another project.
        with SessionLocal() as db:
            if job_id:
                job = db.get(Job, job_id)
                if job and job.status in {'queued', 'running'}:
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


if __name__ == '__main__':
    main()
