from app.db import Job, Problem, SessionLocal
from test_zoj import saved_package

ROOT = "/api/zoj-transfer/problems"


def request(client, method, path, values=None):
    return client.request(method, path, json=values) if values is not None else client.request(method, path)


def test_picker_requires_browser_session_not_bridge_identity(client, owner, problem):
    assert client.get(ROOT).status_code == 401
    assert client.get(ROOT, headers={"X-Editor-Identity":"YXV0aG9yQGV4YW1wbGUuY29t"}).status_code == 401
    assert owner.get(ROOT).status_code == 200
    assert client.get('/api/zoj-library/problems').status_code == 404


def test_library_includes_write_access_and_excludes_read_access(client, owner, problem, clients):
    stranger = clients("stranger")
    other = stranger.post("/api/problems", json={"name": "private", "slug": "private"}).json()["id"]
    saved_package(other, stranger)
    stranger.put(f"/api/problems/{other}/accesses", json={"login": "author", "role": "WRITE"})
    rows = request(owner, "GET", ROOT).json()
    assert {r["id"] for r in rows["problems"]} == {problem, other}
    assert rows["email"] == "author@example.com" and rows["account_exists"]
    assert request(owner, "POST", ROOT + "/" + other + "/packages", values={}).status_code == 202
    stranger.put(f"/api/problems/{other}/accesses", json={"login": "author", "role": "READ"})
    assert [r["id"] for r in request(owner, "GET", ROOT).json()["problems"]] == [problem]
    assert request(owner, "POST", ROOT + "/" + other + "/packages", values={}).status_code == 404
    owner.patch(f"/api/problems/{problem}/deleted", json={"deleted": True})
    assert request(owner, "GET", ROOT).json()["total"] == 0


def test_folders_filter_before_pagination_and_are_private(client, owner, problem, clients):
    folder = owner.post("/api/folders", json={"name": "대회"}).json()["id"]
    second = owner.post("/api/problems", json={"name": "100% match", "slug": "percent"}).json()["id"]
    owner.post("/api/folders/move-problems", json={"folder_id": folder, "problem_ids": [problem]})
    path = ROOT + "?folder=" + folder + "&limit=1"
    result = request(owner, "GET", path).json()
    assert result["total"] == 1 and result["problems"][0]["id"] == problem
    assert request(owner, "GET", ROOT + "?folder=root").json()["problems"][0]["id"] == second
    assert request(owner, "GET", ROOT + "?q=%25").json()["total"] == 1
    stranger = clients("stranger")
    foreign = stranger.post("/api/folders", json={"name": "개인"}).json()["id"]
    assert request(owner, "GET", ROOT + "?folder=" + foreign).status_code == 404


def test_committed_package_keeps_private_work_and_normal_api_requires_commit(client, owner, problem):
    job_id, _ = saved_package(problem, owner)
    state = owner.get("/api/problems/" + problem).json()
    state["document"]["info"]["name"] = "아직 공유하지 않은 제목"
    saved = owner.put(f"/api/problems/{problem}/working-copy", json={"version": state["version"], "document": state["document"]}).json()
    assert owner.post(f"/api/problems/{problem}/jobs?from_committed_revision=true", json={"kind": "package", "version": saved["version"], "options": {"export_format": "zoj"}}).status_code == 409
    path = ROOT + "/" + problem + "/packages"
    prepared = request(owner, "POST", path, values={})
    assert prepared.status_code == 409, prepared.text
    assert prepared.json()["error"]["code"] == "commit_required"
    import uuid
    published=owner.post(f'/api/problems/{problem}/publish-zoj',json={"job_id":job_id,"contest_id":str(uuid.uuid4()),"division_id":str(uuid.uuid4()),"problem_code":"A"})
    assert published.status_code==409 and published.json()["error"]["code"]=="commit_required"
    row = request(owner, "GET", ROOT).json()["problems"][0]
    assert row["uncommitted"] and row["name"] == "두 수의 합"
    assert owner.get("/api/problems/" + problem).json()["document"]["info"]["name"] == "아직 공유하지 않은 제목"
    for values in [{"verify": False}, {"email": "stranger@example.com"}]:
        assert request(owner, "POST", path, values=values).status_code == 422


def test_status_checks_permissions_and_current_revision(owner, problem, clients):
    job_id, _ = saved_package(problem, owner)
    path = f"{ROOT}/{problem}/packages/{job_id}"
    assert owner.get(path).json()["result"]["package"]["verified"] is True
    assert clients("stranger").get(path).status_code == 404
    with SessionLocal() as db:
        db.get(Problem, problem).revision += 1
        db.commit()
    assert owner.get(path).json()["error"]["code"] == "library_package_stale"


def test_shared_folder_writers_can_use_library_until_permission_is_removed(client,owner,problem,clients):
    from test_shared_folders import shared,join,add_problem
    space=shared(owner);member=clients('member');join(owner,member,space,'EDITOR','member')
    peer=add_problem(member,space,'peer-owned')
    assert owner.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':space,'share_confirmed':True}).status_code==200
    result=request(owner,'GET',ROOT+'?folder='+space).json()
    assert {p['id'] for p in result['problems']}=={problem,peer}
    assert all(p['folder_id']==space for p in result['problems'])
    assert request(owner,'GET',ROOT+'?folder=root').json()['total']==0
    result=member.get(ROOT+'?folder='+space).json()
    assert {p['id'] for p in result['problems']}=={problem,peer}
    saved_package(problem, owner)
    assert member.post(ROOT+'/'+problem+'/packages',json={}).status_code==202
    assert owner.put('/api/shared-folders/'+space+'/members/'+member.get('/api/auth/me').json()['id'],json={'role':'VIEWER'}).status_code == 200
    assert [p['id'] for p in member.get(ROOT).json()['problems']]==[peer]
    assert member.post(ROOT+'/'+problem+'/packages',json={}).status_code==404


def test_shared_placement_overrides_old_personal_filing(client,owner,problem):
    private=owner.post('/api/folders',json={'name':'Private'}).json()['id']
    owner.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':private})
    space=owner.post('/api/shared-folders',json={'name':'Team'}).json()['id']
    owner.post('/api/folders/move-problems',json={'problem_ids':[problem],'folder_id':space,'share_confirmed':True})
    assert request(owner,'GET',ROOT+'?folder='+private).json()['total']==0
    assert request(owner,'GET',ROOT+'?folder='+space).json()['problems'][0]['folder_id']==space


def test_library_package_combines_selected_sets_and_always_verifies(client, owner, problem):
    import copy
    saved_package(problem, owner)
    state = owner.get('/api/problems/'+problem).json()
    doc = copy.deepcopy(state['document'])
    doc['testsets']['tle'] = copy.deepcopy(doc['testsets']['tests'])
    saved = owner.put(f'/api/problems/{problem}/working-copy', json={'version':state['version'],'document':doc})
    assert saved.status_code == 200, saved.text
    committed = owner.post(f'/api/problems/{problem}/commit', json={'version':saved.json()['version'],'message':'Add TLE set'})
    assert committed.status_code == 200, committed.text
    response = request(owner, 'POST', ROOT+'/'+problem+'/packages', values={'testsets':['tests','tle']})
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        job = db.get(Job, response.json()['id'])
        assert job.options['testsets'] == ['tests','tle'] and job.options['verify'] is True
        from app.domain import document_hash
        assert job.document_hash == document_hash(job.document)
    assert request(owner, 'POST', ROOT+'/'+problem+'/packages', values={'testsets':[]}).status_code == 422
    assert request(owner, 'POST', ROOT+'/'+problem+'/packages', values={'testsets':['tests'],'verify':False}).status_code == 422
