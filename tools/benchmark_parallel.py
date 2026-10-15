import os,tempfile,json,time,copy,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
root=Path(tempfile.mkdtemp(prefix='editor-parallel-benchmark-'))
os.environ.update(EDITOR_ENV='test',EDITOR_AUTH_MODE='local',EDITOR_EXECUTION_TRANSPORT='local',EDITOR_DATA_DIR=str(root),EDITOR_WORK_DIR=str(root/'work'),EDITOR_DATABASE_URL='sqlite:///'+str(root/'db.sqlite'))
from types import SimpleNamespace
from app.db import Base,engine,SessionLocal,User,Problem,uid
from app.demo import populate
from app.storage import put_blob
from app.pipeline import Pipeline
from app.domain import document_hash
Base.metadata.create_all(engine)
with SessionLocal() as db:
 u=User(email='benchmark@example.com',handle='benchmark',display_name='Local benchmark');db.add(u);db.flush()
 problem=Problem(owner_id=u.id,slug='parallel-benchmark',document={});db.add(problem);db.flush()
 doc=populate(db,problem)
 tests=doc['testsets']['tests']['tests'];template=copy.deepcopy(tests['1']);tests.clear()
 for i in range(1,7):
  inp=f'{i} {i+10}\n'.encode();ans=f'{2*i+10}\n'.encode()
  tests[str(i)]={**template,'index':i,'sample':False,'input_sha':put_blob(db,problem.id,inp)['sha256'],'answer_sha':put_blob(db,problem.id,ans)['sha256']}
 problem.document=doc;db.commit();pid=problem.id;user_id=u.id
options={'testset':'tests','solutions':[],'test_indices':[],'snapshot':'committed','reference':'main.cpp','candidate':'wrong.py','candidates':['wrong.py'],'generator':'gen {seed}','iterations':6,'stop_on_first':False,'counterexample_limit':10}
job=SimpleNamespace(id=uid(),problem_id=pid,user_id=user_id,kind='verify',document=doc,document_hash=document_hash(doc),options=options)
p=Pipeline(job,lambda r:None,lambda:None)
report={'environment':'Local Docker runtime, warm compile cache, not production judge hardware','cases':6,'solutions':2,'stress_iterations':6,'measurements':[]}
try:
 for name in doc['files']:p.program(name)
 for name in doc['solutions']:p.program(name,True)
 p.program(doc['checker'])
 for kind in ('verify','stress'):
  baseline=None
  for width in (1,4):
   p.parallelism=width;p.job.kind=kind;p.result={'summary':'','events':[],'testsets':{},'solutions':[],'unit_tests':[]};p.last_publish=0
   start=time.monotonic();getattr(p,kind)();elapsed=time.monotonic()-start
   if kind=='verify': fingerprint=[(s['name'],s['matched'],[(c['index'],c['verdict'],c['actual_sha']) for c in s['cases']]) for s in p.result['solutions']]
   else:fingerprint=[(c['seed'],c['input_sha'],c['answer_sha'],[(f['solution'],f['verdict'],f['actual_sha']) for f in c['findings']]) for c in p.result['counterexamples']]
   if baseline is None:baseline=fingerprint
   assert baseline==fingerprint, 'Verdicts or outputs changed'
   row={'kind':kind,'parallelism':width,'seconds':round(elapsed,3),'same_results':True}
   report['measurements'].append(row);print(json.dumps(row),flush=True)
finally:p.sandbox.close()
out=Path(sys.argv[1]);out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print('report:',out,flush=True)
