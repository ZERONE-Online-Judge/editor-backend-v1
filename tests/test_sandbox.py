import os
import pytest
from app.db import SessionLocal
from app.storage import put_blob
from app.sandbox import Sandbox

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(os.environ.get("RUN_SANDBOX_TESTS") != "1", reason="Set RUN_SANDBOX_TESTS=1 after building Editor runtime images")]


def source(problem, name, code, language="python3"):
    with SessionLocal() as db:
        blob=put_blob(db,problem,code.encode())
        db.commit()
        return {"name":name,"kind":"source","language":language,**blob}


def test_python_stdout_and_cpu_metrics(problem):
    sandbox=Sandbox()
    try:
        program=sandbox.compile(source(problem,"sum.py","a,b=map(int,input().split()); print(a+b)\n"),[])
        result=sandbox.run(program,b"1 2\n",memory_mb=64)
        assert result["status"]=="ok" and result["exit_code"]==0
        assert result["output"]==b"3\n"
        assert result["cpu_ms"]>0 and result["memory_kb"]>0
    finally:sandbox.close()


def test_code_cannot_modify_supervisor_evidence_or_read_config(problem):
    code='''
from pathlib import Path
for name in ['../.output','../.editor-job.json']:
    try:
        Path(name).write_text('FAKE')
        raise RuntimeError('wrote supervisor control file')
    except PermissionError:
        pass
print('SAFE')
'''
    sandbox=Sandbox()
    try:
        program=sandbox.compile(source(problem,"attack.py",code),[])
        result=sandbox.run(program,memory_mb=64)
        assert result["exit_code"]==0, result
        assert result["output"]==b"SAFE\n"
    finally:sandbox.close()


def test_busy_loop_and_memory_limit_are_real(problem):
    sandbox=Sandbox()
    try:
        loop=sandbox.compile(source(problem,"loop.py","while True: pass\n"),[])
        result=sandbox.run(loop,cpu_ms=250,memory_mb=64)
        assert result["status"]=="time-limit"
        assert result["cpu_ms"]>=240
        memory=sandbox.compile(source(problem,"memory.py","a=[]\nwhile True: a.append(bytearray(1024*1024))\n"),[])
        result=sandbox.run(memory,cpu_ms=1000,memory_mb=32)
        assert result["status"]=="memory-limit", result
    finally:sandbox.close()


def test_cpp_and_java_compile_and_run(problem):
    sandbox=Sandbox()
    try:
        for name,language,code in [('sum.cpp','cpp17','#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b<<"\\n";}'),('Main.java','java17','import java.util.*;public class Main {public static void main(String[] args){Scanner s=new Scanner(System.in);System.out.println(s.nextLong()+s.nextLong());}}')]:
            program=sandbox.compile(source(problem,name,code,language),[])
            result=sandbox.run(program,b"10 20\n",cpu_ms=1000,memory_mb=256)
            assert result['status']=='ok' and result['exit_code']==0,result
            assert result['output']==b'30\n'
    finally:sandbox.close()


def test_grader_and_packaged_java_entry(problem):
    sandbox=Sandbox()
    try:
        grader=source(problem,'grader.cpp','#include <iostream>\nlong long solve(long long,long long);int main(){long long a,b;std::cin>>a>>b;std::cout<<solve(a,b)<<"\\n";}','cpp17')
        grader['compile_with_solution']=True
        contestant=source(problem,'solution.cpp','long long solve(long long a,long long b){return a+b;}','cpp17')
        compiled=sandbox.compile(contestant,[grader],is_solution=True)
        assert sandbox.run(compiled,b'40 2\n')['output']==b'42\n'
        java=source(problem,'Solution.java','package example;public class Solution{public static void main(String[] a){System.out.println(42);}}','java17')
        compiled=sandbox.compile(java,[],is_solution=True)
        result=sandbox.run(compiled,memory_mb=256)
        assert result['output']==b'42\n', result
    finally:sandbox.close()


def test_interactive_private_input_and_duplex_output(problem):
    sandbox=Sandbox()
    try:
        candidate=source(problem,'interactive.py','x=int(input());print(x*2,flush=True)\n')
        actor=source(problem,'interactor.py','import sys\nx=int(open(sys.argv[1]).read());print(x,flush=True)\nans=int(input());assert ans==2*x\n')
        a=sandbox.compile(candidate,[]); b=sandbox.compile(actor,[])
        result=sandbox.interactive(a,b,b'21\n',cpu_ms=1000,memory_mb=128)
        assert result['status']=='ok' and result['exit_code']==0 and result['interactor_exit_code']==0,result
    finally:sandbox.close()
