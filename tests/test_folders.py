from app.db import Access, Problem, SessionLocal, User

def create(client, name, parent=None):
    response = client.post('/api/folders', json={'name':name,'parent_id':parent})
    assert response.status_code == 201, response.text
    return response.json()['id']

def test_personal_organization_and_atomic_move(owner, clients, problem):
    reader=clients('reader')
    with SessionLocal() as db:
        u=db.query(User).filter_by(handle='reader').one()
        db.add(Access(problem_id=problem,user_id=u.id,role='READ'));db.commit()
    folder=create(owner,'대회'); child=create(owner,'예선',folder)
    assert owner.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':child}).status_code==200
    assert owner.get('/api/problems').json()[0]['folder_id']==child
    assert reader.get('/api/problems').json()[0]['folder_id'] is None
    assert reader.get('/api/folders').json()==[]
    assert reader.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':folder}).status_code==404
    own=create(reader,'검수')
    assert reader.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':own}).status_code==200
    assert owner.post('/api/folders/move-problems',json={'problem_ids':[problem,'missing'],'folder_id':folder}).status_code==404
    assert owner.get('/api/problems').json()[0]['folder_id']==child
    with SessionLocal() as db: assert db.get(Problem,problem).revision==0
    assert owner.delete('/api/folders/'+folder).status_code==200
    assert owner.get('/api/folders').json()[0]['parent_id'] is None
    assert owner.delete('/api/folders/'+child).status_code==200
    assert owner.get('/api/problems').json()[0]['folder_id'] is None
    assert reader.get('/api/problems').json()[0]['folder_id']==own

def test_cycles_duplicate_names_creation_and_delete_collision(owner, problem):
    a=create(owner,'2026'); b=create(owner,'Final',a)
    assert owner.put('/api/folders/'+a,json={'name':'2026','parent_id':b}).status_code==400
    assert owner.post('/api/folders',json={'name':'  final ','parent_id':a}).status_code==409
    root=create(owner,'Final')
    assert owner.delete('/api/folders/'+a).status_code==409
    assert len(owner.get('/api/folders').json())==3
    response=owner.post('/api/problems',json={'name':'문제 B','slug':'problem-b','folder_id':b})
    assert response.status_code==201 and response.json()['folder_id']==b
    assert owner.delete('/api/folders/'+root).status_code==200
    assert owner.delete('/api/folders/'+a).status_code==200
    assert owner.get('/api/problems/'+response.json()['id']).json()['problem']['folder_id']==b

def test_depth_validation_includes_moved_subtree(owner):
    parent=None
    for i in range(12): parent=create(owner,f'level-{i}',parent)
    assert owner.post('/api/folders',json={'name':'too-deep','parent_id':parent}).status_code==400
    outer=create(owner,'outer');create(owner,'inner',outer)
    assert owner.put('/api/folders/'+outer,json={'name':'outer','parent_id':parent}).status_code==400


def test_concurrent_inverse_moves_cannot_create_cycle(owner):
    from concurrent.futures import ThreadPoolExecutor
    a=create(owner,'A');b=create(owner,'B')
    def move(ids):
        source,target,name=ids
        return owner.put('/api/folders/'+source,json={'name':name,'parent_id':target}).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        statuses=list(workers.map(move,[(a,b,'A'),(b,a,'B')]))
    assert sorted(statuses)==[200,400]
