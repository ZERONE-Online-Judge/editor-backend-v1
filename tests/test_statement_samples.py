from types import SimpleNamespace
import pytest
from app.schema import Test as EditorTest
from app.statement_samples import display, validate
from app.sandbox import SandboxError
from app.domain import expanded_tests


def test_display_values_are_independent_and_empty_is_a_real_override(monkeypatch):
    monkeypatch.setattr('app.statement_samples.read_blob',lambda sha: b'original\n')
    row={'input_sha':'input','answer_sha':'answer','statement_input':'shown\n','statement_output':''}
    assert display(row,'input')=='shown\n' and display(row,'output')==''
    del row['statement_output']
    assert display(row,'output')=='original\n'
    assert 'statement_input' not in EditorTest(index=1,command='gen').model_dump()


def test_display_validation_executes_validator_main_and_checker_without_mutation():
    calls=[]
    p=SimpleNamespace(doc={'info':{'interactive':False}},
        validator=lambda inp,ts,g:(calls.append(('validator',inp,ts,g)) or True,{}),
        run_solution=lambda name,inp,**kw:(calls.append(('main',inp)) or 'AC',{'output':b'5\n'}),
        checker=lambda inp,out,ref,points:(calls.append(('checker',inp,out,ref)) or 'AC',{}))
    test={'index':1,'sample':True,'group':'small','statement_input':'2 3\n','statement_output':'5 \n'}
    before=dict(test)
    assert validate(p,test,'tests',b'1 2\n',b'3\n','main')=='VALID'
    assert calls[-1]==('checker',b'2 3\n',b'5 \n',b'5\n') and test==before
    p.checker=lambda *args: ('WA',{'stderr':'wrong'})
    with pytest.raises(SandboxError,match='Checker'): validate(p,test,'tests',b'1 2\n',b'3\n','main')


def test_interactive_samples_require_explicit_manual_verification_choice():
    p=SimpleNamespace(doc={'info':{'interactive':True}})
    row={'sample':True,'statement_output':'? 1\n! 2\n'}
    with pytest.raises(SandboxError,match='자동 검증'): validate(p,row,'tests',b'',b'','main')
    row['verify_statement_io']=False
    assert validate(p,row,'tests',b'',b'','main')=='NOT_REQUESTED'


def test_script_expansion_does_not_drop_display_metadata_from_existing_tests():
    config={'script':'gen 1 > 1','groups_enabled':False,'groups':{},'tests':{'1':{'index':1,'command':'gen 1','sample':True,'statement_output':'preview','verify_statement_io':False}}}
    row=expanded_tests(config)[0]
    assert row['sample'] and row['statement_output']=='preview' and not row['verify_statement_io']
