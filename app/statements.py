"""Shared statement sections and content-based translation freshness."""
import hashlib
import json

FIELDS=('name','legend','input','output','scoring','interaction','notes','tutorial')


def effective(doc,language):
    local=dict(doc['statements'][language])
    for key,common in [('legend','common_statement'),('tutorial','common_tutorial')]:
        local[key]='\n\n'.join(value for value in [doc.get(common,''),local.get(key,'')] if value)
    return local


def fingerprint(doc):
    language=doc.get('translation_source')
    if not language or language not in doc['statements']: return None
    value={'language':language,'statement':{key:doc['statements'][language].get(key,'') for key in FIELDS},
           'common_statement':doc.get('common_statement',''),'common_tutorial':doc.get('common_tutorial','')}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def translation_status(doc):
    current=fingerprint(doc)
    return {language: 'source' if language==doc.get('translation_source') else 'untracked' if not current else
            'unreviewed' if language not in doc.get('translation_reviews',{}) else
            'stale' if doc['translation_reviews'][language]['source_hash']!=current else
            'current' if doc['translation_reviews'][language]['target_hash']==statement_fingerprint(doc,language) else 'modified'
            for language in doc['statements']}


def statement_fingerprint(doc,language):
    value={'language':language,'statement':{key:doc['statements'][language].get(key,'') for key in FIELDS}}
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
