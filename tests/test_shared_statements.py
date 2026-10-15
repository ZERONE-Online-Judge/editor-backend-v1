import copy
from app.schema import empty_document, Document
from app.statements import effective, fingerprint, statement_fingerprint, translation_status


def fixture():
    doc=empty_document('공유');doc['statements']['korean']['legend']='정수 $N$'
    doc['statements']['english']={**doc['statements']['korean'],'name':'Shared','legend':'N'}
    doc.update(common_statement='공통 $N$',common_tutorial='SECRET_COMMON',translation_source='korean')
    return doc


def test_shared_sections_and_content_fingerprints_preserve_translation_freshness():
    doc=fixture()
    assert fingerprint(doc)=='ffdb9a14348e743a44a1ad73170944afd369784d2b0f3ab325857abb829a501c'
    assert statement_fingerprint(doc,'english')=='1027d4b5e644e683f43916316918f4760d62b6ea30f7d096d56a56c3dd552630'
    doc['translation_reviews']={'english':{'source_hash':fingerprint(doc),'target_hash':statement_fingerprint(doc,'english')}}
    assert translation_status(doc)['english']=='current'
    doc['statements']['english']['legend']='Changed translation'
    assert translation_status(doc)['english']=='modified'
    doc['statements']['korean']['legend']='Changed original'
    assert translation_status(doc)['english']=='stale'
    assert effective(doc,'english')['tutorial']=='SECRET_COMMON'
    assert doc['statements']['english']['tutorial']==''
    assert 'common_statement' not in Document.model_validate(empty_document('Legacy')).model_dump()


def test_translation_review_stamps_must_match_both_current_original_and_translation(owner,problem):
    state=owner.get(f'/api/problems/{problem}').json();state['document']=fixture()
    state['document']['translation_reviews']={'english':{'source_hash':'a'*64,'target_hash':'b'*64}}
    response=owner.put(f'/api/problems/{problem}/working-copy',json={'version':state['version'],'document':state['document']})
    assert response.status_code==400 and response.json()['error']['code']=='translation_review_stale'
    doc=state['document'];doc['translation_reviews']['english']={'source_hash':fingerprint(doc),'target_hash':statement_fingerprint(doc,'english')}
    assert owner.put(f'/api/problems/{problem}/working-copy',json={'version':state['version'],'document':doc}).status_code==200


def test_translator_cannot_remove_common_fields_or_edit_original_via_missing_keys(owner,problem,clients):
    state=owner.get(f'/api/problems/{problem}').json();doc=fixture()
    saved=owner.put(f'/api/problems/{problem}/working-copy',json={'version':state['version'],'document':doc}).json()
    assert owner.post(f'/api/problems/{problem}/commit',json={'version':saved['version'],'message':'Shared source'}).status_code==200
    translator=clients('translation-user')
    assert owner.put(f'/api/problems/{problem}/accesses',json={'login':'translation-user','role':'READ','translator':True}).status_code==200
    state=translator.get(f'/api/problems/{problem}').json()
    changed=copy.deepcopy(state['document']);del changed['common_statement']
    assert translator.put(f'/api/problems/{problem}/working-copy',json={'version':state['version'],'document':changed}).status_code==403
    changed=copy.deepcopy(state['document']);changed['statements']['korean']['legend']='unauthorized'
    assert translator.put(f'/api/problems/{problem}/working-copy',json={'version':state['version'],'document':changed}).status_code==403
    changed=copy.deepcopy(state['document']);changed['statements']['english']['legend']='Valid translation edit'
    changed['translation_reviews']={'english':{'source_hash':fingerprint(changed),'target_hash':statement_fingerprint(changed,'english')}}
    assert translator.put(f'/api/problems/{problem}/working-copy',json={'version':state['version'],'document':changed}).status_code==200
