import copy
import pytest
from app.db import Job, Problem, SessionLocal


def seed(owner, problem):
    state = owner.get(f'/api/problems/{problem}').json()
    with SessionLocal() as db:
        actor = db.get(Problem, problem).owner_id
        categories = ['prepare-full', 'prepare-inputs', 'prepare-validate', 'prepare-answers',
                      'verify', 'validator-tests', 'checker-tests', 'stress', 'render', 'package']
        for round in range(5):
            for index, category in enumerate(categories):
                mode = category.removeprefix('prepare-')
                db.add(Job(id=f'{round}-{index}', problem_id=problem, user_id=actor,
                    kind='prepare' if category.startswith('prepare-') else category, status='completed',
                    document=copy.deepcopy(state['document']), document_hash='d'*64, workspace_version=0,
                    revision=0, created_at=round,
                    options={'prepare_mode':mode, 'source_document_hash':state['document_hash'] if round % 2 == 0 else 'e'*64},
                    result={}))
        # Older jobs omitted full mode, or kept the mode only in their result.
        for name, options, result in [('legacy-full',{},{}), ('legacy-inputs',{}, {'prepare_mode':'inputs'})]:
            db.add(Job(id=name, problem_id=problem, user_id=actor, kind='prepare', status='completed',
                document=copy.deepcopy(state['document']), document_hash='d'*64, workspace_version=0,
                revision=0, created_at=9, options=options, result=result))
        db.commit()
    return state['document_hash']


@pytest.mark.parametrize('category,count', [('prepare-full',6), ('prepare-inputs',6), ('prepare-validate',5),
    ('prepare-answers',5), ('prepare',22), ('verify',5), ('validator-tests',5), ('checker-tests',5),
    ('stress',5), ('render',5), ('package',5)])
def test_specific_categories_filter_before_pagination_and_serialize_consistently(owner,problem,category,count):
    seed(owner,problem)
    seen=[]
    for page in range(1, (count+2)//3+1):
        response=owner.get(f'/api/problems/{problem}/jobs',params={'category':category,'page':page,'page_size':3})
        assert response.status_code==200,response.text
        data=response.json()
        assert data['total']==count
        assert all(row['category'].startswith('prepare-') if category=='prepare' else row['category']==category for row in data['items'])
        seen.extend(row['id'] for row in data['items'])
    assert len(seen)==len(set(seen))==count


def test_category_counts_match_material_filter_and_legacy_detail(owner,problem):
    source_hash=seed(owner,problem)
    response=owner.get(f'/api/problems/{problem}/jobs',params={
        'category':'prepare-inputs','page':1,'current_hash':source_hash,'relation':'current'})
    assert response.status_code==200,response.text
    data=response.json()
    assert data['total']==3 and data['counts']=={'all':6,'current':3,'other':3}
    detail=owner.get(f'/api/problems/{problem}/jobs/legacy-inputs').json()
    assert detail['category']=='prepare-inputs'
    assert owner.get(f'/api/problems/{problem}/jobs?category=unknown').status_code==422


def test_category_filter_preserves_problem_authorization(owner,problem,clients):
    seed(owner,problem)
    outsider=clients('outsider')
    assert outsider.get(f'/api/problems/{problem}/jobs?category=prepare-inputs&page=1').status_code in {403,404}
