import pytest
from pydantic import ValidationError
from app.resources import compile_resources
from app.schema import SourceFile


def resource(name, **extra):
    return {'name':name,'kind':'resource','language':'text','sha256':'a'*64,'size':1,**extra}


def test_resource_scope_is_enforced_by_language_and_solution_target():
    policy={'languages':['cpp17','cpp20'],'targets':['SOLUTION'],'stages':['COMPILE']}
    files=[resource('grader.h',resource_policy=policy),resource('common.h'),resource('notes.txt',kind='aux',attach_to_build=False),resource('legacy.txt',kind='aux')]
    assert [r['name'] for r in compile_resources(files,'cpp17',True)]==['grader.h','common.h','legacy.txt']
    assert [r['name'] for r in compile_resources(files,'python3',True)]==['common.h','legacy.txt']
    assert [r['name'] for r in compile_resources(files,'cpp17',False)]==['common.h','legacy.txt']


def test_resource_policy_rejects_unsupported_targets_stages_and_empty_languages():
    for policy in [{'languages':[]},{'languages':['cpp17'],'stages':['RUN']},{'languages':['cpp17'],'targets':['CHECKER']}]:
        with pytest.raises(ValidationError): SourceFile.model_validate(resource('helper.h',resource_policy=policy))
    with pytest.raises(ValidationError): SourceFile.model_validate(resource('helper.h',kind='aux',resource_policy={'languages':['cpp17']}))
    legacy=SourceFile.model_validate(resource('legacy.txt',kind='aux')).model_dump()
    assert 'resource_policy' not in legacy and 'attach_to_build' not in legacy
