"""Exercise the actual compiler/interpreter and isolated resource accounting."""
import os
import pytest
from app.sandbox import Sandbox, SandboxError
from test_sandbox import source

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(os.environ.get('RUN_SANDBOX_TESTS') != '1', reason='Actual runtime image required')]

CASES = [
    ('sum.py', 'pypy3', 'a,b=map(int,input().split());print(a+b)\n', 'while True: pass\n', 'a=[]\nwhile True: a.append(bytearray(1024*1024))\n', 'def broken(:'),
    ('Main.kt', 'kotlin', 'fun main(){val(a,b)=readln().split(" ").map{it.toLong()};println(a+b)}', 'fun main(){while(true){}}', 'fun main(){val a=ArrayList<ByteArray>();while(true){a.add(ByteArray(1024*1024))}}', 'fun main( {'),
]

@pytest.mark.parametrize('name,language,correct,loop,memory,invalid', CASES)
def test_actual_icpc_runtime_accepts_output_and_enforces_limits(problem, name, language, correct, loop, memory, invalid):
    sandbox=Sandbox()
    try:
        program=sandbox.compile(source(problem,name,correct,language),[])
        result=sandbox.run(program,b'40 2\n',cpu_ms=2000,memory_mb=256)
        assert result['status']=='ok' and result['exit_code']==0,result
        assert result['output']==b'42\n' and result['cpu_ms']>0 and result['memory_kb']>0
        # Compilation artifacts must survive a separate execution container.
        assert (program.artifacts/'program.jar').is_file() if language=='kotlin' else program.command[0]=='/usr/local/bin/pypy3'
        program=sandbox.compile(source(problem,'Loop'+('.kt' if language=='kotlin' else '.py'),loop,language),[])
        result=sandbox.run(program,cpu_ms=350,memory_mb=256)
        assert result['status']=='time-limit',result
        program=sandbox.compile(source(problem,'Memory'+('.kt' if language=='kotlin' else '.py'),memory,language),[])
        result=sandbox.run(program,cpu_ms=3000,memory_mb=96)
        assert result['status']=='memory-limit',result
        with pytest.raises(SandboxError,match='컴파일 실패'):
            sandbox.compile(source(problem,'Invalid'+('.kt' if language=='kotlin' else '.py'),invalid,language),[])
    finally:sandbox.close()
