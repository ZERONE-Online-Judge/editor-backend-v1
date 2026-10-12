import copy
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.db import Access, Job, Problem, Revision, SessionLocal, User, WorkingCopy
from app.demo import populate
from app.domain import document_hash
from app.zoj_package import build_zoj_package
from app.config import get_settings
from app import zoj


def saved_package(problem, owner):
    with SessionLocal() as db:
        p=db.get(Problem,problem);p.document=populate(db,p)
        db.get(Revision,(problem,0)).document=copy.deepcopy(p.document)
        u=db.get(User,p.owner_id)
        job=Job(problem_id=problem,user_id=u.id,kind='package',status='completed',document=p.document,
                document_hash=document_hash(p.document),workspace_version=0,revision=0,
                options={'export_format':'zoj','testset':'tests','statement_language':'korean','verify':True})
        db.add(job);db.flush()
        package=build_zoj_package(job,{'tests':list(p.document['testsets']['tests']['tests'].values())})
        job.result={'package':package};db.commit()
        return job.id, get_settings().data_dir/'packages'/(job.id+'.zip')


def test_zoj_manifest_assets_samples_checksum_and_editor_roundtrip(owner,problem):
    job_id,path=saved_package(problem,owner)
    with zipfile.ZipFile(path) as z:
        m=json.loads(z.read('manifest.json'))
        assert m['format']=='zoj-problem' and m['version']==1
        assert set(z.namelist())=={'manifest.json',*(f['path'] for f in m['files'])}
        for f in m['files']:
            data=z.read(f['path']);assert len(data)==f['size'];assert hashlib.sha256(data).hexdigest()==f['sha256']
        assert len(m['testcase_sets'][0]['testcases'])==3 and m['testcase_sets'][0]['is_active']
        meta=json.loads(m['problem']['statement'].split('<!--ZOJ_META:',1)[1].split('-->')[0])
        assert meta['examples'][0]['input']=='1 2\n' and meta['examples'][0]['output']=='3\n'
        categories={a['category'] for a in m['assets']}
        assert {'package-files/checker','package-files/validator','package-files/main-solution','verification-solutions/wrong_answer','package-files/package-resource'}<=categories
    folder=owner.post('/api/folders',json={'name':'복원'}).json()['id']
    response=owner.post('/api/imports?name=복원&slug=restored&folder_id='+folder,content=path.read_bytes())
    assert response.status_code==201,response.text
    restored=owner.get('/api/problems/'+response.json()['id']).json()
    assert restored['document']['validator_tests'] and restored['document']['solutions']['wrong.py']['tag']=='WA'
    assert restored['problem']['folder_id']==folder


@pytest.mark.parametrize("mode", ["points", "cpp20_checker"])
def test_export_rejects_unsupported_judging_instead_of_silently_changing_it(owner,problem,mode):
    saved_package(problem,owner)
    state=owner.get('/api/problems/'+problem).json()
    if mode=='points':
        state['document']['info']['points_enabled']=True
    else:
        state['document']['checker']='validator.cpp'
        state['document']['files']['validator.cpp']['language']='cpp20'
    response=owner.put('/api/problems/'+problem+'/working-copy',json={'version':state['version'],'document':state['document']})
    assert response.status_code==200
    owner.post('/api/problems/'+problem+'/commit',json={'version':response.json()['version'],'message':'부분점수'})
    state=owner.get('/api/problems/'+problem).json()
    r=owner.post('/api/problems/'+problem+'/jobs',json={'kind':'package','version':state['version'],'options':{'export_format':'zoj'}})
    assert r.status_code==400 and r.json()['error']['code']=='zoj_incompatible'


def test_publish_checks_owner_current_revision_and_artifact_and_reuses_request(owner,clients,problem,monkeypatch):
    job_id,path=saved_package(problem,owner)
    writer=clients('writer')
    with SessionLocal() as db:
        db.add(Access(problem_id=problem,user_id=db.query(User).filter_by(handle='writer').one().id,role='WRITE'));db.commit()
    values={'job_id':job_id,'contest_id':str(uuid.uuid4()),'division_id':str(uuid.uuid4()),'problem_code':'A'}
    calls=[]
    def send(method,route,user,package=None,sha=None):
        calls.append((method,route,user.email,sha))
        return {'problem_id':str(uuid.uuid4()),'title':'두 수의 합'}
    monkeypatch.setattr(zoj,'remote',send)
    assert writer.post('/api/problems/'+problem+'/publish-zoj',json=values).status_code==403
    assert owner.post('/api/problems/'+problem+'/publish-zoj',json={**values,'email':'another@example.com'}).status_code==422
    first=owner.post('/api/problems/'+problem+'/publish-zoj',json=values);assert first.status_code==200,first.text
    second=owner.post('/api/problems/'+problem+'/publish-zoj',json=values);assert second.status_code==200
    assert calls[0][1]==calls[1][1] and calls[0][2]=='author@example.com'
    assert len(owner.get('/api/problems/'+problem+'/zoj-publications').json())==1
    path.write_bytes(b'changed')
    assert owner.post('/api/problems/'+problem+'/publish-zoj',json=values).status_code==409
    with SessionLocal() as db:
        p=db.get(Problem,problem);p.revision+=1;db.commit()
    assert owner.post('/api/problems/'+problem+'/publish-zoj',json=values).json()['error']['code']=='stale_package'


def test_unconfigured_service_and_fixed_server_targets(owner):
    assert owner.get('/api/zoj/contests').json()['configured'] is False
    from app.config import Settings
    for url in ['https://zoj.kr/api?key=1','http://user:pass@host/api','https://zoj.kr/api/../auth']:
        with pytest.raises(ValueError): Settings(zoj_api_url=url)


def test_editor_restore_survives_zoj_reexported_file_paths(owner,problem):
    _,path=saved_package(problem,owner)
    with zipfile.ZipFile(path) as z:
        manifest=json.loads(z.read('manifest.json'))
        files={n:z.read(n) for n in z.namelist() if n!='manifest.json'}
    renamed={name:'reexport/'+name for name in files}
    for f in manifest['files']:f['path']=renamed[f['path']]
    for a in manifest['assets']:a['file']=renamed[a['file']]
    for s in manifest['testcase_sets']:
        for t in s['testcases']:
            for field in ['input','output']:t[field]=renamed[t[field]]
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('manifest.json',json.dumps(manifest).encode())
        for name,data in files.items():z.writestr(renamed[name],data)
    response=owner.post('/api/imports?name=Returned&slug=returned-from-zoj',content=result.getvalue())
    assert response.status_code==201,response.text
    restored=owner.get('/api/problems/'+response.json()['id']).json()['document']
    assert len(restored['testsets']['tests']['tests'])==3 and restored['solutions']['wrong.py']['tag']=='WA'
