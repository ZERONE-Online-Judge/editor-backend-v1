"""Explicit local demo data; never invoked at production startup."""
import copy
from sqlalchemy import select
from .db import Access, Base, Problem, Revision, SessionLocal, User, engine
from .schema import empty_document, Document
from .storage import put_blob

MAIN = '#include <iostream>\nusing namespace std;\nint main(){long long a,b;cin>>a>>b;cout<<a+b<<"\\n";}\n'
VALIDATOR = '#include "testlib.h"\nint main(int argc,char**argv){registerValidation(argc,argv);inf.readLong(-1000000000LL,1000000000LL,"a");inf.readSpace();inf.readLong(-1000000000LL,1000000000LL,"b");inf.readEoln();inf.readEof();}\n'
GENERATOR = '#include "testlib.h"\n#include <iostream>\nusing namespace std;\nint main(int argc,char**argv){registerGen(argc,argv,1);cout<<rnd.next(-1000000,1000000)<<" "<<rnd.next(-1000000,1000000)<<"\\n";}\n'
WRONG = 'a,b=map(int,input().split())\nprint(a-b)\n'


def populate(db, problem):
    doc = empty_document("두 수의 합")
    doc["info"].update(tags=["구현", "기초"], description="Editor 실행 흐름을 확인하는 로컬 예제 문제")
    doc["statements"]["korean"].update(legend="두 정수 $a$와 $b$가 주어집니다. 두 수의 합 $a+b$를 구하세요.", input="첫째 줄에 두 정수 $a$, $b$가 공백으로 구분되어 주어집니다.\n\n$-10^9 \\le a,b \\le 10^9$", output="두 정수의 합을 출력하세요.", tutorial="64비트 정수로 입력을 받고 $a+b$를 출력하면 됩니다. 시간 복잡도는 $O(1)$입니다.")
    def source(name, code, language="cpp17"):
        return {"name": name, "kind": "source", "language": language, **put_blob(db, problem.id, code.encode())}
    doc["files"]["validator.cpp"] = source("validator.cpp", VALIDATOR)
    doc["files"]["gen.cpp"] = source("gen.cpp", GENERATOR)
    doc["validator"] = "validator.cpp"
    doc["solutions"]["main.cpp"] = {**source("main.cpp", MAIN), "tag": "MA", "extra_tags": {}}
    doc["solutions"]["wrong.py"] = {**source("wrong.py", WRONG, "python3"), "tag": "WA", "extra_tags": {}}
    for index, inp, ans, sample in [(1,"1 2\n","3\n",True),(2,"-10 20\n","10\n",False),(3,"1000000000 1000000000\n","2000000000\n",False)]:
        doc["testsets"]["tests"]["tests"][str(index)] = {"index":index,"input_sha":put_blob(db, problem.id, inp.encode())["sha256"],"answer_sha":put_blob(db, problem.id, ans.encode())["sha256"],"command":"","group":"","points":0,"description":"예제" if sample else "경계값 검사","sample":sample}
    for index, inp, expected in [(1,"1 2\n","VALID"),(2,"1000000001 1\n","INVALID")]:
        doc["validator_tests"][str(index)] = {"input_sha":put_blob(db,problem.id,inp.encode())["sha256"],"output_sha":None,"answer_sha":None,"expected":expected}
    return Document.model_validate(doc).model_dump()


def seed():
    from .config import get_settings
    if get_settings().env == "production":
        raise RuntimeError("Demo seeding is disabled in production")
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "editor@example.com"))
        if not user:
            user=User(email="editor@example.com",handle="zoj-editor",display_name="ZOJ Editor")
            db.add(user);db.flush()
        problem = db.scalar(select(Problem).where(Problem.owner_id==user.id,Problem.slug=="a-plus-b"))
        if problem:
            print("Local example already exists:", problem.id)
            return
        problem=Problem(owner_id=user.id,slug="a-plus-b",document=empty_document("두 수의 합"))
        db.add(problem);db.flush()
        problem.document=populate(db,problem)
        db.add(Access(problem_id=problem.id,user_id=user.id,role="OWNER"))
        db.add(Revision(problem_id=problem.id,number=0,author_id=user.id,message="로컬 실행 확인용 예제",document=copy.deepcopy(problem.document)))
        db.commit()
        print("Local example created:",problem.id)
        print("Sign in using editor@example.com. Development codes are in data/development-mail.")


if __name__=="__main__":
    seed()
