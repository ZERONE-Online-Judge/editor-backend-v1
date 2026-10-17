"""Direct and inherited capability enforcement, not just checkbox payloads."""
import copy
from types import SimpleNamespace
import pytest
from app.db import SessionLocal, Access, WorkspaceMember, Job, Revision
from app.permissions import grants, has, stored, PROBLEM, EDIT, job_allowed
from test_shared_folders import shared, add_problem


def grant(owner, pid, login, permissions):
    r = owner.put(f'/api/problems/{pid}/accesses', json={'login': login, 'permissions': permissions})
    assert r.status_code == 200, r.text


def team(owner, member, space, permissions, handle='member'):
    r = owner.post(f'/api/shared-folders/{space}/invitations', json={'email':handle+'@example.com','permissions':permissions})
    assert r.status_code == 201, r.text
    assert member.post(f'/api/shared-folders/invitations/{r.json()["id"]}/accept').status_code == 200
    return member.get('/api/auth/me').json()['id']


@pytest.mark.parametrize('scope,field,value', [
    ('edit_info','info',None), ('edit_statements','common_statement','New statement'),
    ('edit_files','checker','std::ncmp'), ('edit_solutions','solutions',{}),
    ('edit_tests','testsets',{}), ('edit_materials','materials',{}),
])
def test_each_edit_scope_can_save_and_commit_only_its_section(owner, clients, scope, field, value):
    pid=owner.post('/api/problems',json={'name':'Permissions','slug':'permissions'}).json()['id']
    user=clients('member'); grant(owner,pid,'member',[scope])
    data=user.get(f'/api/problems/{pid}').json(); doc=copy.deepcopy(data['document'])
    if field=='info': doc['info']['name']='Allowed'
    elif field=='solutions':
        content=b'print(1)\n'
        blob=user.post(f'/api/problems/{pid}/blobs',content=content).json()
        doc['solutions']={'main.py':{'name':'main.py','language':'python3','sha256':blob['sha256'],'size':len(content),'tag':'MA'}}
    elif field=='materials':
        doc[field]={'public':{'strategy':'NONE','files':[],'solutions':[],'testsets':[]}}
    elif field=='testsets':
        doc[field]={}
    else: doc[field]=value
    r=user.put(f'/api/problems/{pid}/working-copy',json={'version':0,'document':doc})
    assert r.status_code==200,r.text
    bad=copy.deepcopy(doc)
    bad['info']['name']='Forbidden' if field!='info' else doc['info']['name']
    if field=='info': bad['common_statement']='Forbidden'
    r=user.put(f'/api/problems/{pid}/working-copy',json={'version':r.json()['version'],'document':bad})
    assert r.status_code==403,r.text
    current=user.get(f'/api/problems/{pid}').json()
    r=user.post(f'/api/problems/{pid}/commit',json={'version':current['version'],'message':'Allowed edit'})
    assert r.status_code==200,r.text
    assert r.json()['committed']
    assert user.post(f'/api/problems/{pid}/jobs',json={'kind':'render','version':0}).status_code==403
    assert user.post(f'/api/problems/{pid}/issues',json={'title':'No','content':'No'}).status_code==403
    assert user.put(f'/api/problems/{pid}/accesses',json={'login':'author','permissions':[]}).status_code==403


def test_read_only_does_not_fall_back_to_legacy_write_and_revoked_draft_cannot_commit(owner,clients,problem):
    pid=problem; user=clients('member'); grant(owner,pid,'member',['edit_info','edit_statements'])
    data=user.get(f'/api/problems/{pid}').json();data['document']['common_statement']='Uncommitted'
    r=user.put(f'/api/problems/{pid}/working-copy',json={'version':0,'document':data['document']});assert r.status_code==200
    grant(owner,pid,'member',['edit_info'])
    assert user.post(f'/api/problems/{pid}/commit',json={'version':1,'message':'Stale'}).status_code==403
    assert user.post(f'/api/problems/{pid}/commit-preview',json={'version':1,'document':data['document']}).status_code==403
    grant(owner,pid,'member',[])
    record=user.get(f'/api/problems/{pid}').json()
    assert record['problem']['permissions']==[]
    assert not record['document'].get('common_statement')
    assert user.put(f'/api/problems/{pid}/working-copy',json={'version':1,'document':data['document']}).status_code==403


def test_direct_manager_cannot_escalate_or_edit_stronger_member(owner,clients,problem):
    manager=clients('manager'); peer=clients('peer');grant(owner,problem,'manager',['manage_access','issues'])
    grant(owner,problem,'peer',['edit_info'])
    endpoint=f'/api/problems/{problem}/accesses'
    assert manager.put(endpoint,json={'login':'manager','permissions':['manage_access','publish']}).status_code==403
    assert manager.put(endpoint,json={'login':'peer','role':'NONE'}).status_code==403
    assert manager.put(endpoint,json={'login':'peer','role':'WRITE'}).status_code==403
    other=clients('other')
    assert manager.put(endpoint,json={'login':'other','permissions':['issues']}).status_code==200
    assert other.post(f'/api/problems/{problem}/issues',json={'title':'Allowed','content':'Comment'}).status_code==201
    assert manager.put(endpoint,json={'login':'author','permissions':[]}).status_code==400
    assert owner.put(endpoint,json={'login':'peer','permissions':['unknown']}).status_code==422
    assert owner.put(endpoint,json={'login':'peer','permissions':['manage_folders']}).status_code==400


def test_folder_grants_union_and_revocation_never_materialize_direct_shares(owner,clients):
    member=clients('member'); space=shared(owner);pid=add_problem(owner,space)
    ident=team(owner,member,space,['edit_tests','execute','issues'])
    grant(owner,pid,'member',['edit_statements'])
    data=member.get(f'/api/problems/{pid}').json()['problem']
    assert set(data['permissions'])=={'edit_tests','execute','issues','edit_statements'}
    assert data['inherited_permissions']==['edit_tests','execute','issues']
    assert data['direct_permissions']==['edit_statements']
    assert member.put(f'/api/problems/{pid}/accesses',json={'login':'author','permissions':[]}).status_code==403
    assert owner.put(f'/api/shared-folders/{space}/members/{ident}',json={'permissions':[]}).status_code==200
    data=member.get(f'/api/problems/{pid}').json()['problem']
    assert data['permissions']==['edit_statements']
    assert owner.delete(f'/api/shared-folders/{space}/members/{ident}').status_code==200
    assert member.get(f'/api/problems/{pid}').status_code==200


def test_folder_creation_and_organization_are_independent(owner,clients):
    member=clients('member');space=shared(owner); ident=team(owner,member,space,['create_problems'])
    assert member.post('/api/problems',json={'name':'Created','slug':'created','folder_id':space}).status_code==201
    assert member.post('/api/folders',json={'name':'Denied','parent_id':space}).status_code==403
    owner.put(f'/api/shared-folders/{space}/members/{ident}',json={'permissions':['manage_folders']})
    assert member.post('/api/folders',json={'name':'Allowed','parent_id':space}).status_code==201
    assert member.post('/api/problems',json={'name':'Denied','slug':'denied','folder_id':space}).status_code==403
    folder=next(f for f in member.get('/api/folders').json() if f['id']==space)
    assert folder['can_edit'] and not folder['can_create'] and not folder['can_manage']


def test_team_manager_may_only_delegate_current_grants_and_pending_invites_recheck(owner,clients):
    space=shared(owner);manager=clients('member');ident=team(owner,manager,space,['manage_access','issues'])
    other=clients('other')
    endpoint=f'/api/shared-folders/{space}/invitations'
    assert manager.post(endpoint,json={'email':'other@example.com','permissions':['publish']}).status_code==403
    assert manager.post(endpoint,json={'email':'other@example.com','role':'ADMIN'}).status_code==403
    pending=manager.post(endpoint,json={'email':'other@example.com','permissions':['issues']})
    assert pending.status_code==201
    owner.put(f'/api/shared-folders/{space}/members/{ident}',json={'permissions':['manage_access']})
    assert other.post(f'/api/shared-folders/invitations/{pending.json()["id"]}/accept').status_code==409
    assert manager.put(f'/api/shared-folders/{space}/members/{ident}',json={'permissions':['manage_access','publish']}).status_code==403


def test_execution_package_prepare_and_publication_are_separate(owner,clients,problem):
    user=clients('member');pid=problem
    grant(owner,pid,'member',['package'])
    r=user.post(f'/api/problems/{pid}/jobs',json={'kind':'package','version':0,'options':{'verify':False}})
    assert r.status_code==202,r.text
    assert r.json()['options']['snapshot']=='committed'
    assert user.post(f'/api/problems/{pid}/jobs',json={'kind':'prepare','version':0}).status_code==403
    assert user.post(f'/api/problems/{pid}/jobs',json={'kind':'stress','version':0}).status_code==403
    assert user.post(f'/api/problems/{pid}/publish-zoj',json={'contest_id':'11111111-1111-1111-1111-111111111111','division_id':'22222222-2222-2222-2222-222222222222','problem_code':'A','job_id':r.json()['id']}).status_code==403
    grant(owner,pid,'member',['prepare'])
    r=user.post(f'/api/problems/{pid}/jobs',json={'kind':'prepare','version':0,'options':{'auto_apply':True}})
    assert r.status_code==403,r.text
    grant(owner,pid,'member',['render'])
    r=user.post(f'/api/problems/{pid}/jobs',json={'kind':'render','version':0})
    assert r.status_code==202,r.text
    assert r.json()['options']['snapshot']=='committed'


@pytest.mark.parametrize('kind,scope',[('verify','execute'),('stress','execute'),('prepare','prepare'),('package','package'),('render','render')])
def test_worker_rechecks_specific_permission(kind,scope):
    access=SimpleNamespace(role='WRITE',permissions=stored([scope]))
    job=SimpleNamespace(kind=kind,options={'snapshot':'committed'})
    assert job_allowed(access,job)
    access.permissions={}
    assert not job_allowed(access,job)
    access.permissions=stored(['edit_info'])
    assert not job_allowed(access,job)


def test_legacy_grants_and_explicit_empty_are_distinct():
    old=SimpleNamespace(role='WRITE',permissions=None,reviewer=False,translator=False)
    assert EDIT<=grants(old) and has(old,'publish')
    old.permissions={}
    assert grants(old)==set()
    old.role='OWNER'
    assert grants(old)==PROBLEM


def test_zoj_library_selection_uses_publish_instead_of_edit_role(owner,clients,problem):
    from sqlalchemy import select
    from app.db import Problem
    from app.folder_access import writable_problem_ids
    user=clients('member'); ident=user.get('/api/auth/me').json()['id']
    grant(owner,problem,'member',list(EDIT))
    with SessionLocal() as db:
        assert problem not in db.scalars(select(Problem.id).where(Problem.id.in_(writable_problem_ids(ident)))).all()
    grant(owner,problem,'member',['publish'])
    with SessionLocal() as db:
        assert problem in db.scalars(select(Problem.id).where(Problem.id.in_(writable_problem_ids(ident)))).all()
    grant(owner,problem,'member',[])
    with SessionLocal() as db:
        assert problem not in db.scalars(select(Problem.id).where(Problem.id.in_(writable_problem_ids(ident)))).all()


def test_pending_worker_cannot_execute_removed_edit_scope():
    access=SimpleNamespace(role='READ',permissions=stored(['execute','edit_info']))
    job=SimpleNamespace(kind='verify',options={'snapshot':'working_copy','permission_requirements':['execute','edit_tests']})
    assert not job_allowed(access,job)
    access.permissions=stored(['execute','edit_tests'])
    assert job_allowed(access,job)
