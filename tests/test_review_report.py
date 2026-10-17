from types import SimpleNamespace
from app.schema import empty_document
from app.storage import put_blob
from app.db import SessionLocal, Job, JobApplication, WorkingCopy, now
from app.domain import document_hash
from app.review_report import build


def setup_doc(problem):
    doc=empty_document('Bounds')
    with SessionLocal() as db:
        def blob(value): return put_blob(db,problem,value.encode())
        validator=blob('#include "testlib.h"\nint main(int argc,char**argv){registerValidation(argc,argv);\nint n=inf.readInt(1, 100, "N"); inf.readEof();}')
        inp=blob('50\n'); main=blob('int main(){}')
        doc['files']['val.cpp']={'name':'val.cpp','language':'cpp17','kind':'source',**validator}
        doc['validator']='val.cpp';doc['checker']='std::wcmp.cpp'
        doc['statements']['korean']['legend']='$1 \\le N \\le 200$'
        doc['testsets']['tests']['tests']={'1':{'index':1,'input_sha':inp['sha256'],'answer_sha':inp['sha256'],'command':'','group':'','points':0,'sample':False,'description':''}}
        doc['solutions']['main.cpp']={'name':'main.cpp','language':'cpp17','kind':'source','tag':'MA','extra_tags':{},**main}
        db.commit()
    return doc


def test_report_proves_duplicates_bounds_and_missing_maximum(problem):
    doc=setup_doc(problem)
    doc['testsets']['tests']['tests']['2']={**doc['testsets']['tests']['tests']['1'],'index':2}
    report=build(doc)
    codes={item['code'] for item in report['items']}
    assert {'CONSTRAINT_DIFFERENCE','NO_MAXIMUM_INPUT','DUPLICATE_INPUT','NO_EXECUTION'}<=codes
    doc['testsets']['tests']['tests']['2']['input_sha']=None
    assert 'NO_MAXIMUM_INPUT' not in {item['code'] for item in build(doc)['items']}
    doc['statements']['korean']['legend']='$1 \\le N \\le 200 * K$'
    assert 'CONSTRAINT_DIFFERENCE' not in {item['code'] for item in build(doc)['items']}


def test_report_never_treats_partial_scope_as_all_ac_or_old_jobs_as_current(owner,problem):
    doc=setup_doc(problem)
    scope={'testset':'tests','group':'','tag':'WA','matched':False,'complete':False,'tested':1,'total':2,'verdicts':['AC']}
    job=SimpleNamespace(id='e'*32,kind='verify',status='completed',result={'solutions':[{'name':'wrong.cpp','expectations':[scope],'expectations_complete':False}]})
    assert 'EXPECTED_VERDICT_MISMATCH' not in {i['code'] for i in build(doc,[job])['items']}
    scope['complete']=True;scope['total']=1
    assert 'EXPECTED_VERDICT_MISMATCH' in {i['code'] for i in build(doc,[job])['items']}
    with SessionLocal() as db:
        wc=db.query(WorkingCopy).filter_by(problem_id=problem).first()
        if wc is None:
            owner.get(f'/api/problems/{problem}');wc=db.query(WorkingCopy).filter_by(problem_id=problem).first()
        wc.document=doc
        old=Job(problem_id=problem,user_id=wc.user_id,kind='verify',status='completed',document=doc,document_hash='f'*64,workspace_version=1,revision=0,options={},result=job.result,finished_at=now())
        db.add(old);db.commit();old_id=old.id;user_id=wc.user_id
    url=f'/api/problems/{problem}/review-report'
    first=owner.get(url);assert first.status_code==200
    assert old_id not in first.json()['evidence_jobs']
    with SessionLocal() as db:
        db.add(JobApplication(job_id=old_id,user_id=user_id,workspace_version=2,document_hash=document_hash(doc)));db.commit()
    second=owner.get(url).json()
    assert old_id in second['evidence_jobs']
    assert any(i['code']=='EXPECTED_VERDICT_MISMATCH' for i in second['items'])
    second['items'].clear()
    assert owner.get(url).json()['items']


def test_report_does_not_parse_commented_validator_or_cross_user_access(owner,problem,clients):
    doc=setup_doc(problem)
    with SessionLocal() as db:
        validator=put_blob(db,problem,b'// inf.readInt(1, 200, "N");\nint main(){registerValidation(argc,argv); if(flag){inf.readInt(1, 100, "N");}}')
        doc['files']['val.cpp'].update(validator);db.commit()
    codes={item['code'] for item in build(doc)['items']}
    assert 'CONSTRAINT_DIFFERENCE' not in codes and 'NO_MAXIMUM_INPUT' not in codes
    assert clients('outsider').get(f'/api/problems/{problem}/review-report').status_code in {403,404}


def test_realistic_latex_thousands_and_indexed_variables_have_no_false_mismatch(problem):
    doc=setup_doc(problem)
    doc['statements']['korean']['legend']=r'$1 \le K \le 1\,000$; $0 \le A_{i} \le 10^{3}$'
    with SessionLocal() as db:
        blob=put_blob(db,problem,b'#define MAX_K 1000\nint main(){registerValidation(argc,argv);int k=inf.readInt(1,MAX_K,"K");for(int i=0;i<k;i++){inf.readInt(0,1000,"A_i");}}')
        doc['files']['val.cpp'].update(blob);db.commit()
    report=build(doc)
    assert not any(i['code']=='CONSTRAINT_DIFFERENCE' for i in report['items'])
    assert any(i['code']=='CONSTRAINT_SCOPE' and '2개' in i['message'] for i in report['items'])
    doc['statements']['korean']['legend']=r'$1 \le K \le 10^{4}$'
    difference=next(i for i in build(doc)['items'] if i['code']=='CONSTRAINT_DIFFERENCE')
    assert '10,000' in difference['evidence'] and '1,000' in difference['evidence'] and 'MAX_K' in difference['evidence']


def test_conflicting_translations_or_symbolic_limits_are_excluded(problem):
    doc=setup_doc(problem)
    doc['statements']['english']={**doc['statements']['korean'],'legend':r'$1 \le N \le 100$'}
    assert not any(i['code']=='CONSTRAINT_DIFFERENCE' for i in build(doc)['items'])
    del doc['statements']['english']
    doc['statements']['korean']['legend']=r'$1 \le N \le 1000 + K$'
    assert not any(i['code']=='CONSTRAINT_DIFFERENCE' for i in build(doc)['items'])
