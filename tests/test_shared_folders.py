import copy
import pytest
from sqlalchemy import select
from app.db import SessionLocal, WorkspaceInvitation, Access, SharedPlacement, Folder, Problem, Revision, Job, now
from app.worker import claim_job, process_job


def shared(owner, name="대회 공동 작업"):
    r = owner.post('/api/shared-folders', json={'name':name})
    assert r.status_code == 201, r.text
    return r.json()['id']


def join(owner, member, space, role, handle):
    r = owner.post(f'/api/shared-folders/{space}/invitations',json={'email':handle+'@example.com','role':role})
    assert r.status_code == 201, r.text
    r = member.post(f'/api/shared-folders/invitations/{r.json()["id"]}/accept')
    assert r.status_code == 200, r.text


def add_problem(owner, space, slug='shared-task'):
    r = owner.post('/api/problems',json={'name':'공동 문제','slug':slug,'folder_id':space})
    assert r.status_code == 201, r.text
    return r.json()['id']


@pytest.mark.parametrize('role,write,review,translate', [
    ('ADMIN',True,False,False), ('EDITOR',True,False,False),
    ('REVIEWER',False,True,False), ('TRANSLATOR',False,False,True), ('VIEWER',False,False,False)])
def test_roles_inherit_through_descendants_and_new_problems(owner,clients,role,write,review,translate):
    space=shared(owner); member=clients('member')
    child=owner.post('/api/folders',json={'name':'하위','parent_id':space}).json()['id']
    pid=add_problem(owner,child)
    assert member.get(f'/api/problems/{pid}').status_code==404
    join(owner,member,space,role,'member')
    assert {x['id'] for x in member.get('/api/folders').json()}=={space,child}
    record=member.get(f'/api/problems/{pid}').json()
    assert record['problem']['role']==('WRITE' if write else 'READ')
    assert record['problem']['reviewer']==review and record['problem']['translator']==translate
    assert not record['problem']['can_manage_access']
    assert member.get('/api/problems').json()[0]['id']==pid
    new=add_problem(owner,child,'new-task')
    assert member.get(f'/api/problems/{new}').status_code==200
    doc=copy.deepcopy(record['document']);doc['info']['name']='변경 시도'
    assert member.put(f'/api/problems/{pid}/working-copy',json={'version':0,'document':doc}).status_code==(200 if write else 403)
    r=member.post('/api/problems',json={'name':'추가','slug':'member-task','folder_id':child})
    assert r.status_code==(201 if write else 403)
    assert member.post(f'/api/problems/{pid}/jobs',json={'kind':'render','version':1 if write else 0}).status_code==(202 if write or review or translate else 403)
    assert member.post(f'/api/problems/{pid}/issues',json={'title':'검토','content':'의견'}).status_code==(201 if write or review or translate else 403)
    with SessionLocal() as db:
        from app.db import User
        member_id = db.scalar(select(User.id).where(User.handle=='member'))
        assert db.get(Access,(pid,member_id)) is None
        assert db.scalar(select(Access).where(Access.problem_id==pid,Access.role=='OWNER'))


def test_invitation_is_account_bound_expiring_revocable_and_requires_acceptance(owner,clients):
    space=shared(owner); member=clients('member'); outsider=clients('outsider'); pid=add_problem(owner,space)
    invite=owner.post(f'/api/shared-folders/{space}/invitations',json={'email':' MEMBER@example.com ','role':'EDITOR'}).json()['id']
    assert member.get(f'/api/problems/{pid}').status_code==404
    assert outsider.get('/api/shared-folders/invitations').json()==[]
    assert outsider.post(f'/api/shared-folders/invitations/{invite}/accept').status_code==404
    assert member.get('/api/shared-folders/invitations').json()[0]['folder_name']=='대회 공동 작업'
    assert owner.delete(f'/api/shared-folders/{space}/invitations/{invite}').status_code==200
    assert member.post(f'/api/shared-folders/invitations/{invite}/accept').status_code==409
    owner.post(f'/api/shared-folders/{space}/invitations',json={'email':'member@example.com','role':'VIEWER'})
    with SessionLocal() as db:
        db.get(WorkspaceInvitation,invite).expires_at=now()-1;db.commit()
    assert member.get('/api/shared-folders/invitations').json()==[]
    assert member.post(f'/api/shared-folders/invitations/{invite}/accept').status_code==409
    join(owner,member,space,'VIEWER','member')
    assert member.post(f'/api/shared-folders/invitations/{invite}/accept').status_code==409
    assert outsider.get(f'/api/shared-folders/{space}/members').status_code==404
    assert member.post(f'/api/shared-folders/{space}/invitations',json={'email':'outsider@example.com'}).status_code==403


def test_revocation_retains_direct_access_but_cancels_active_work(owner,clients,monkeypatch):
    space=shared(owner);member=clients('member');join(owner,member,space,'EDITOR','member')
    pid=add_problem(owner,space); second=add_problem(owner,space,'second-task')
    who=next(x for x in owner.get(f'/api/shared-folders/{space}/members').json()['members'] if x['handle']=='member')['user_id']
    assert owner.put(f'/api/problems/{second}/accesses',json={'login':'member','role':'READ'}).status_code==200
    job=member.post(f'/api/problems/{pid}/jobs',json={'kind':'render','version':0}).json()
    running=claim_job();assert running.id==job['id']
    assert owner.delete(f'/api/shared-folders/{space}/members/{who}').status_code==200
    assert member.get(f'/api/problems/{pid}').status_code==404
    assert member.get(f'/api/problems/{second}').json()['problem']['role']=='READ'
    assert member.get('/api/folders').json()==[]
    class FakePipeline:
        def __init__(self,job,publish,check): self.check=check
        def run(self): self.check();raise AssertionError('revoked job executed')
    monkeypatch.setattr('app.worker.Pipeline',FakePipeline)
    process_job(running)
    with SessionLocal() as db:
        assert db.get(Job,job['id']).status=='cancelled'


def test_no_permanent_resharing_or_owner_privilege_via_shared_editor(owner,clients,problem):
    space=shared(owner);member=clients('member')
    join(owner,member,space,'EDITOR','member');pid=add_problem(owner,space)
    assert member.put(f'/api/problems/{pid}/accesses',json={'login':'member','role':'WRITE'}).status_code==403
    assert member.post('/api/folders/move-problems',json={'problem_ids':[pid],'folder_id':None,'share_confirmed':True}).status_code==403
    assert member.patch(f'/api/problems/{pid}/deleted',json={'deleted':True}).status_code==403
    response = member.post(f'/api/problems/{pid}/publish-zoj',json={'job_id':'a'*32,'contest_id':'12345678-1234-1234-1234-123456789abc','division_id':'12345678-1234-1234-1234-123456789abc','problem_code':'A'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'zoj_package_required'
    assert member.post('/api/folders',json={'name':'Private'}).status_code==201
    assert owner.put(f'/api/problems/{problem}/accesses',json={'login':'member','role':'WRITE'}).status_code==200
    assert member.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':space,'share_confirmed':True}).status_code==403
    assert member.get(f'/api/problems/{pid}').json()['problem']['workspace_id']==space


def test_move_is_atomic_confirmed_and_updates_shared_visibility(owner,clients,problem):
    a=shared(owner,'A'); b=shared(owner,'B');first=clients('first');second=clients('second')
    join(owner,first,a,'VIEWER','first');join(owner,second,b,'VIEWER','second')
    move=lambda ids,folder,confirmed=False:owner.post('/api/folders/move-problems',json={'problem_ids':ids,'folder_id':folder,'share_confirmed':confirmed})
    assert move([problem],a).status_code==409
    assert first.get(f'/api/problems/{problem}').status_code==404
    assert move([problem,'f'*32],a,True).status_code==404
    assert first.get(f'/api/problems/{problem}').status_code==404
    assert move([problem],a,True).status_code==200
    assert first.get(f'/api/problems/{problem}').status_code==200
    assert move([problem],b,True).status_code==200
    assert first.get(f'/api/problems/{problem}').status_code==404
    assert second.get(f'/api/problems/{problem}').status_code==200
    assert move([problem],None,True).status_code==200
    assert second.get(f'/api/problems/{problem}').status_code==404


def test_tree_scope_root_protection_and_subfolder_removal(owner,clients):
    a=shared(owner,'A');b=shared(owner,'B');member=clients('member');join(owner,member,a,'ADMIN','member')
    child=member.post('/api/folders',json={'name':'Child','parent_id':a}).json()['id']
    pid=add_problem(owner,child)
    assert owner.put(f'/api/folders/{child}',json={'name':'Child','parent_id':b}).status_code==400
    assert member.put(f'/api/folders/{a}',json={'name':'Renamed'}).status_code==200
    assert owner.put(f'/api/folders/{a}',json={'name':'A','parent_id':child}).status_code==403
    assert owner.delete(f'/api/folders/{a}').status_code==409
    assert member.delete(f'/api/folders/{child}').status_code==200
    assert owner.get('/api/problems').json()[0]['folder_id']==a
    people=member.get(f'/api/shared-folders/{a}/members').json()['members']
    founder=next(x['user_id'] for x in people if x['role']=='OWNER')
    assert member.delete(f'/api/shared-folders/{a}/members/{founder}').status_code==403
    assert member.put(f'/api/shared-folders/{a}/members/{founder}',json={'role':'VIEWER'}).status_code==403
    assert owner.post('/api/folders/move-problems',json={'problem_ids':[pid],'folder_id':None,'share_confirmed':True}).status_code==200
    assert member.delete(f'/api/folders/{a}').status_code==403
    assert owner.delete(f'/api/folders/{a}').status_code==200
    assert member.get('/api/folders').json()==[]


def test_role_downgrade_revokes_execution_and_temporary_privilege(owner,clients):
    space=shared(owner);member=clients('member');join(owner,member,space,'EDITOR','member')
    pid=add_problem(owner,space)
    ident=next(x['user_id'] for x in owner.get(f'/api/shared-folders/{space}/members').json()['members'] if x['handle']=='member')
    state=member.get(f'/api/problems/{pid}').json();doc=state['document'];doc['info']['name']='Privileged unsaved draft'
    assert member.put(f'/api/problems/{pid}/working-copy',json={'version':0,'document':doc}).status_code==200
    assert owner.put(f'/api/shared-folders/{space}/members/{ident}',json={'role':'TRANSLATOR'}).status_code==200
    assert member.post(f'/api/problems/{pid}/commit',json={'version':1,'message':'Cannot smuggle old author edits'}).status_code==403
    doc['info']['name']='Another unauthorized edit'
    assert member.put(f'/api/problems/{pid}/working-copy',json={'version':1,'document':doc}).status_code==403
    assert member.post(f'/api/problems/{pid}/jobs',json={'kind':'verify','version':1}).status_code==403
    assert owner.put(f'/api/shared-folders/{space}/members/{ident}',json={'role':'VIEWER'}).status_code==200
    assert member.get(f'/api/problems/{pid}').json()['document']['info']['name']=='공동 문제'
    assert member.post(f'/api/problems/{pid}/jobs',json={'kind':'render','version':1}).status_code==403
    assert member.post(f'/api/problems/{pid}/issues',json={'title':'No','content':'No'}).status_code==403


def test_import_and_reviewer_invocation_inherit_folder_policy(owner,clients):
    import io,json,zipfile
    from app.schema import empty_document
    space=shared(owner);member=clients('member');join(owner,member,space,'REVIEWER','member')
    archive=io.BytesIO()
    with zipfile.ZipFile(archive,'w') as z:z.writestr('editor.json',json.dumps({"format":"zoj-editor-v1","document":empty_document('Imported')}))
    url=f'/api/imports?name=Imported&slug=imported&folder_id={space}'
    assert member.post(url,content=archive.getvalue()).status_code==403
    imported=owner.post(url,content=archive.getvalue());assert imported.status_code==201,imported.text
    pid=imported.json()['id'];assert member.get(f'/api/problems/{pid}').status_code==200
    from app.demo import populate
    with SessionLocal() as db:
        row=db.get(Problem,pid)
        doc=populate(db,row);doc['info']['name']='Imported'
        row.document=doc;db.get(Revision,(pid,0)).document=copy.deepcopy(doc);db.commit()
    draft=owner.get(f'/api/problems/{pid}').json();draft['document']['info']['name']='Uncommitted owner changes'
    assert owner.put(f'/api/problems/{pid}/working-copy',json={'version':0,'document':draft['document']}).status_code==200
    requested=member.post(f'/api/problems/{pid}/jobs',json={'kind':'verify','version':0})
    assert requested.status_code==202,requested.text
    with SessionLocal() as db:
        job=db.get(Job,requested.json()['id'])
        assert job.options['snapshot']=='committed' and job.document['info']['name']=='Imported'
    assert member.post(f'/api/problems/{pid}/jobs',json={'kind':'prepare','version':0}).status_code==403


def test_demoted_inviter_cannot_leave_a_late_access_grant(owner,clients):
    space=shared(owner);admin=clients('admin');recipient=clients('recipient')
    join(owner,admin,space,'ADMIN','admin')
    invite=admin.post(f'/api/shared-folders/{space}/invitations',json={'email':'recipient@example.com','role':'EDITOR'}).json()['id']
    ident=next(x['user_id'] for x in owner.get(f'/api/shared-folders/{space}/members').json()['members'] if x['handle']=='admin')
    owner.put(f'/api/shared-folders/{space}/members/{ident}',json={'role':'VIEWER'})
    assert recipient.post(f'/api/shared-folders/invitations/{invite}/accept').status_code==409
    assert recipient.get('/api/folders').json()==[]


def test_shared_tree_concurrent_inverse_moves_and_leave(owner,clients):
    from concurrent.futures import ThreadPoolExecutor
    space=shared(owner);member=clients('member');join(owner,member,space,'EDITOR','member')
    a=member.post('/api/folders',json={'name':'A','parent_id':space}).json()['id']
    b=member.post('/api/folders',json={'name':'B','parent_id':space}).json()['id']
    def move(values):
        source,target,name=values
        return member.put('/api/folders/'+source,json={'name':name,'parent_id':target}).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        statuses=list(workers.map(move,[(a,b,'A'),(b,a,'B')]))
    assert sorted(statuses)==[200,400]
    ident=next(x['user_id'] for x in member.get(f'/api/shared-folders/{space}/members').json()['members'] if x['handle']=='member')
    assert member.delete(f'/api/shared-folders/{space}/members/{ident}').status_code==200
    assert member.get('/api/folders').json()==[]


def test_concurrent_acceptance_and_invitation_revocation_do_not_report_false_removal(owner,clients):
    from concurrent.futures import ThreadPoolExecutor
    space=shared(owner);member=clients('member')
    invite=owner.post(f'/api/shared-folders/{space}/invitations',json={'email':'member@example.com','role':'VIEWER'}).json()['id']
    def accept_or_cancel(accepting):
        return (member.post(f'/api/shared-folders/invitations/{invite}/accept') if accepting else owner.delete(f'/api/shared-folders/{space}/invitations/{invite}')).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        accepted,revoked=list(workers.map(accept_or_cancel,[True,False]))
    assert (accepted,revoked) in {(200,409),(409,200)}
    assert bool(member.get('/api/folders').json())==(accepted==200)


def test_pending_invitation_permissions_apply_only_after_acceptance(owner, clients):
    space = shared(owner)
    member = clients('member')
    pid = add_problem(owner, space)
    invitation = owner.post(f'/api/shared-folders/{space}/invitations', json={
        'email': 'member@example.com', 'role': 'EDITOR',
    }).json()['id']
    with SessionLocal() as db:
        row = db.get(WorkspaceInvitation, invitation)
        original = (row.email, row.created_at, row.expires_at)
    endpoint = f'/api/shared-folders/{space}/invitations/{invitation}'
    response = owner.put(endpoint, json={'permissions': ['execute', 'issues']})
    assert response.status_code == 200, response.text
    received = member.get('/api/shared-folders/invitations').json()[0]
    assert received['permissions'] == ['execute', 'issues']
    assert member.get(f'/api/problems/{pid}').status_code == 404
    # Empty means read-only, even when the same invitation was originally EDITOR.
    assert owner.put(endpoint, json={'permissions': []}).status_code == 200
    with SessionLocal() as db:
        row = db.get(WorkspaceInvitation, invitation)
        assert (row.email, row.created_at, row.expires_at) == original
        assert row.status == 'pending' and row.permissions == {}
    assert member.post(f'/api/shared-folders/invitations/{invitation}/accept').status_code == 200
    assert member.get(f'/api/problems/{pid}').json()['problem']['permissions'] == []
    assert owner.put(endpoint, json={'permissions': ['issues']}).status_code == 409
    assert member.get(f'/api/problems/{pid}').json()['problem']['permissions'] == []


@pytest.mark.parametrize('state', ['expired', 'revoked', 'declined'])
def test_invitation_permission_edit_cannot_revive_processed_invites(owner, clients, state):
    space = shared(owner)
    member = clients('member')
    invitation = owner.post(f'/api/shared-folders/{space}/invitations', json={
        'email': 'member@example.com', 'permissions': ['issues'],
    }).json()['id']
    with SessionLocal() as db:
        row = db.get(WorkspaceInvitation, invitation)
        if state == 'expired':
            row.expires_at = now() - 1
        else:
            row.status = state
        db.commit()
    endpoint = f'/api/shared-folders/{space}/invitations/{invitation}'
    assert owner.put(endpoint, json={'role': 'ADMIN'}).status_code == 409
    assert member.get('/api/shared-folders/invitations').json() == []
    assert member.post(f'/api/shared-folders/invitations/{invitation}/accept').status_code == 409


def test_invitation_permission_edit_checks_actor_scope_and_grant_sponsor(owner, clients):
    from test_feature_permissions import team
    space = shared(owner)
    manager = clients('manager')
    ident = team(owner, manager, space, ['manage_access', 'issues'], 'manager')
    recipient = clients('recipient')
    viewer = clients('viewer')
    team(owner, viewer, space, [], 'viewer')
    invitation = owner.post(f'/api/shared-folders/{space}/invitations', json={
        'email': 'recipient@example.com', 'permissions': ['issues'],
    }).json()['id']
    endpoint = f'/api/shared-folders/{space}/invitations/{invitation}'
    assert viewer.put(endpoint, json={'permissions': []}).status_code == 403
    assert recipient.put(endpoint, json={'permissions': []}).status_code == 404
    assert manager.put(endpoint, json={'permissions': ['publish']}).status_code == 403
    assert owner.put(endpoint, json={'permissions': ['publish']}).status_code == 200
    assert manager.put(endpoint, json={'permissions': []}).status_code == 403
    assert owner.put(endpoint, json={'permissions': ['manage_access']}).status_code == 200
    assert manager.put(endpoint, json={'permissions': []}).status_code == 403
    assert owner.put(endpoint, json={'permissions': ['issues']}).status_code == 200
    assert manager.put(endpoint, json={'permissions': ['issues']}).status_code == 200
    # The current grant is sponsored by the editing manager and rechecked on accept.
    assert owner.put(f'/api/shared-folders/{space}/members/{ident}', json={'permissions': []}).status_code == 200
    assert recipient.post(f'/api/shared-folders/invitations/{invitation}/accept').status_code == 409
    assert owner.put(endpoint, json={'permissions': ['issues']}).status_code == 200
    assert recipient.post(f'/api/shared-folders/invitations/{invitation}/accept').status_code == 200


def test_invitation_permission_edit_rejects_wrong_workspace_and_unknown_permissions(owner, clients):
    first = shared(owner, 'First')
    second = shared(owner, 'Second')
    invitation = owner.post(f'/api/shared-folders/{first}/invitations', json={
        'email': 'member@example.com', 'permissions': [],
    }).json()['id']
    assert owner.put(f'/api/shared-folders/{second}/invitations/{invitation}', json={'permissions': []}).status_code == 404
    endpoint = f'/api/shared-folders/{first}/invitations/{invitation}'
    assert owner.put(endpoint, json={'permissions': ['unknown']}).status_code == 422
