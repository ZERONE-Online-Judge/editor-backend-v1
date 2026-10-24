"""Compare native Main generation and verification on every online language."""
import copy
import json
from uuid import uuid4
from sqlalchemy import delete, select
from .db import Access, Blob, Job, JobApplication, JobDiscard, Problem, Revision, SessionLocal, User, WorkingCopy, now
from .demo import populate
from .jobs import JobRequest, _create_job
from .language_policy_smoke import wait
from .prepared_results import discard_prepared_results
from .schema import Document
from .storage import blob_path, put_blob, read_blob

SOURCES = [
    ('main.cpp', 'cpp17', '#include <cstdio>\nint main(){int a,b;scanf("%d%d",&a,&b);printf("%d\\n",a+b);}\n'),
    ('main.c', 'c99', '#include <stdio.h>\nint main(){int a,b;scanf("%d%d",&a,&b);printf("%d\\n",a+b);}\n'),
    ('main.py', 'python3', 'import time\na,b=map(int,input().split())\nt=time.process_time()\nwhile time.process_time()-t < .35: pass\nprint(a+b)\n'),
    ('main-pypy.py', 'pypy3', 'a,b=map(int,input().split())\nprint(a+b)\n'),
    ('Main.java', 'java8', 'import java.util.Scanner;public class Main{public static void main(String[]args){Scanner s=new Scanner(System.in);System.out.println(s.nextInt()+s.nextInt());}}\n'),
    ('Main.kt', 'kotlin', 'fun main(){val(a,b)=readln().trim().split(" ").map{it.toInt()};println(a+b)}\n'),
]


def run():
    fixture = uuid4().hex
    try:
        with SessionLocal() as db:
            user = db.scalar(select(User).where(User.central_account_id.is_not(None)).order_by(User.created_at).limit(1))
            if not user:
                raise RuntimeError('An existing shared account must sign into Editor first.')
            identity = user.id
            problem = Problem(id=fixture, owner_id=identity, slug='native-reference-'+fixture, document={})
            db.add(problem)
            db.flush()
            base = populate(db,problem)
            base['info'].update(name='Temporary native reference check',time_limit_ms=250,memory_limit_mb=256)
            test = base['testsets']['tests']['tests']['1']
            test.update(command='',input_sha=put_blob(db,fixture,b'1 2\n')['sha256'],answer_sha=None,sample=False)
            base['testsets']['tests']['tests'] = {'1':test}
            problem.document = Document.model_validate(base).model_dump()
            db.add(Access(problem_id=fixture,user_id=identity,role='OWNER'))
            db.add(Revision(problem_id=fixture,number=0,author_id=identity,message='Temporary reference check',document=copy.deepcopy(problem.document)))
            db.add(WorkingCopy(problem_id=fixture,user_id=identity,base_revision=0,base_document=copy.deepcopy(problem.document),document=copy.deepcopy(problem.document)))
            db.commit()
        for version,(name,language,source) in enumerate(SOURCES):
            with SessionLocal() as db:
                user = db.get(User,identity)
                doc = copy.deepcopy(base)
                doc['solutions'] = {name:{'name':name,'kind':'source','language':language,'tag':'MA',**put_blob(db,fixture,source.encode())}}
                doc = Document.model_validate(doc).model_dump()
                wc = db.get(WorkingCopy,(fixture,identity))
                wc.document,wc.version=doc,version
                db.commit()
                created = _create_job(fixture,JobRequest(kind='prepare',version=version,reuse=False),user,db)
            prepared = wait(created['id'])
            answer = prepared['testsets']['tests'][0]
            if read_blob(answer['answer_sha']) != b'3\n' or not any(r.get('runtime')=='reference' and r.get('agent_version')=='0.3.1' for r in prepared.get('execution_runs',[])):
                raise RuntimeError('Main generation omitted native output or agent identity: '+language)
            with SessionLocal() as db:
                user = db.get(User,identity)
                verified = _create_job(fixture,JobRequest(kind='verify',version=version,reuse=False),user,db)
            result = wait(verified['id'])
            solution = result['solutions'][0]
            case = solution['cases'][0]
            if not result['verified'] or case['verdict']!='AC' or case['actual_sha']!=answer['answer_sha']:
                raise RuntimeError('Generation and verification differed: '+language)
            if language=='python3' and min(answer['main_cpu_ms'],case['cpu_ms'])<=250:
                raise RuntimeError('The Python check did not exercise language-adjusted time limits.')
            with SessionLocal() as db:
                user=db.get(User,identity)
                before=copy.deepcopy(db.get(WorkingCopy,(fixture,identity)).document)
                discard_prepared_results(db,db.get(Job,created['id']),user)
                assert db.get(WorkingCopy,(fixture,identity)).document==before
                assert db.get(Job,created['id']).result['testsets']['tests'][0]['answer_sha']==answer['answer_sha']
            print(json.dumps({'language':language,'generation_cpu_ms':answer['main_cpu_ms'],'verification_cpu_ms':case['cpu_ms'],
                              'output_matches':True,'discard_preserves_copy':True,'runs':solution['judge_runs']},ensure_ascii=False),flush=True)
        print('All six languages use matching native generation and verification; temporary fixture removed.',flush=True)
    finally:
        with SessionLocal() as db:
            ids=list(db.scalars(select(Job.id).where(Job.problem_id==fixture)))
            for row in db.scalars(select(Job).where(Job.problem_id==fixture,Job.status.in_(['queued','running']))):
                row.status,row.finished_at='cancelled',now()
            db.commit()
            hashes=list(db.scalars(select(Blob.sha256).where(Blob.problem_id==fixture)))
            for model in (JobApplication,JobDiscard):
                db.execute(delete(model).where(model.job_id.in_(ids)))
            for model in (Job,WorkingCopy,Revision,Access,Blob):
                db.execute(delete(model).where(model.problem_id==fixture))
            db.execute(delete(Problem).where(Problem.id==fixture))
            db.commit()
            for sha in hashes:
                if not db.scalar(select(Blob.sha256).where(Blob.sha256==sha).limit(1)):
                    blob_path(sha).unlink(missing_ok=True)
