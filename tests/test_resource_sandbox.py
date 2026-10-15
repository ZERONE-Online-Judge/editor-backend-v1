import os
import pytest
from app.db import SessionLocal
from app.storage import put_blob
from app.sandbox import Sandbox

pytestmark=[pytest.mark.sandbox,pytest.mark.skipif(os.environ.get('RUN_SANDBOX_TESTS')!='1',reason='Real runtime image required')]


def test_real_language_and_solution_resource_filters_remove_compile_only_files_from_runs(problem):
    with SessionLocal() as db:
        def source(name,code,kind='source',language='cpp17',**extra):
            return {'name':name,'kind':kind,'language':language,**put_blob(db,problem,code.encode()),**extra}
        header=source('only.h','#define RESOURCE_VALUE 17\n',kind='resource',language='text',resource_policy={'languages':['cpp17'],'targets':['SOLUTION'],'stages':['COMPILE']})
        yes=source('yes.cpp','#include "only.h"\n#include <iostream>\n#include <fstream>\nint main(){std::cout<<RESOURCE_VALUE<<" "<<std::ifstream("only.h").good()<<"\\n";}\n')
        no_code='#if __has_include("only.h")\n#error Unexpected scoped header\n#endif\n#include <iostream>\nint main(){std::cout<<20<<"\\n";}\n'
        no=source('no.cpp',no_code,language='cpp20')
        tool=source('tool.cpp',no_code)
        db.commit()
    sandbox=Sandbox()
    try:
        program=sandbox.compile(yes,[header],is_solution=True)
        assert sandbox.run(program)['output']==b'17 0\n'
        assert not (program.artifacts/'only.h').exists()
        assert sandbox.run(sandbox.compile(no,[header],is_solution=True))['output']==b'20\n'
        assert sandbox.run(sandbox.compile(tool,[header],is_solution=False))['output']==b'20\n'
    finally:
        sandbox.close()
