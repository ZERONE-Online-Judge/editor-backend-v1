import copy
import hashlib
import json
import threading
import zipfile
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from app.schema import Document
from app.language_limits import effective_limits, exported_limits
from app.db import SessionLocal, Problem, Revision
from app.zoj_package import build_zoj_package
from app import stress_judging
from test_judge_bridge import fixture_data


@pytest.mark.parametrize("language,time,memory", [("python3",5000,544),("pypy3",5000,640),("java8",3000,528),("kotlin",3000,528)])
def test_empty_limits_and_disable_follow_contest_policy(language,time,memory):
    assert effective_limits(1000,256,language)=={"time_limit_ms":time,"memory_limit_mb":memory}
    assert effective_limits(1000,256,language,{"disable_auto_adjustment":True,"time_limit_ms":700})=={"time_limit_ms":700,"memory_limit_mb":256}


def test_document_preserves_optional_limits_and_old_hash_shape():
    old=Document(info={"name":"sum"}).model_dump()
    assert "language_resource_limits" not in old
    assert "language_resource_limits" not in Document(info={"name":"sum"},language_resource_limits={"pypy3":{}}).model_dump()
    old["language_resource_limits"]={"python3":{"time_limit_ms":700,"disable_auto_adjustment":True}}
    assert Document.model_validate(old).model_dump()["language_resource_limits"]==old["language_resource_limits"]
    assert exported_limits(old["language_resource_limits"])=={"python313":{"time_limit_ms":700,"disable_auto_adjustment":True}}
    old["language_resource_limits"]["python3"]["disable_auto_adjustment"]="false"
    with pytest.raises(ValidationError): Document.model_validate(old)


def test_package_and_judge_snapshot_both_carry_language_policy(problem,tmp_path):
    from app.judge_bridge import archive
    from app.config import get_settings
    p=fixture_data(problem,tmp_path)
    p.doc["language_resource_limits"]={"python3":{"time_limit_ms":700,"disable_auto_adjustment":True},"kotlin":{"memory_limit_mb":768}}
    options={"export_format":"zoj","verify":True,"testset":"tests","statement_language":"korean"}
    job=SimpleNamespace(id="f"*32,problem_id=problem,revision=0,document=p.doc,document_hash="b"*64,options=options)
    build_zoj_package(job,p.result["testsets"])
    with zipfile.ZipFile(get_settings().data_dir/"packages"/(job.id+".zip")) as z:
        manifest=json.loads(z.read("manifest.json"))
    expected=exported_limits(p.doc["language_resource_limits"])
    assert manifest["problem"]["language_resource_limits"]==expected
    path,_=archive(p,"main.cpp","tests",p.result["testsets"]["tests"],0)
    with zipfile.ZipFile(path) as z: assert json.loads(z.read("manifest.json"))["language_resource_limits"]==expected


def test_online_verification_rejects_isolated_backend(owner,problem,tmp_path):
    p=fixture_data(problem,tmp_path)
    with SessionLocal() as db:
        row=db.get(Problem,problem);row.document=copy.deepcopy(p.doc);db.get(Revision,(problem,0)).document=copy.deepcopy(p.doc);db.commit()
    state=owner.get(f"/api/problems/{problem}").json()
    for kind,options in [("verify",{}),("package",{"export_format":"linux","verify":True}),
                         ("stress",{"generator":"gen {seed}","reference":"main.cpp","candidates":["wrong.py"]})]:
        result=owner.post(f"/api/problems/{problem}/jobs",json={"kind":kind,"version":state["version"],"options":{**options,"execution_backend":"editor"}})
        assert result.status_code==400 and result.json()["error"]["code"]=="judge_backend_required"
        result=owner.post(f"/api/problems/{problem}/jobs",json={"kind":kind,"version":state["version"],"options":options})
        assert result.status_code==202 and result.json()["options"]["execution_backend"]=="zoj"
        assert result.json()["options"]["judging_policy"]["effective_limits"]["pypy3"]=={"time_limit_ms":5000,"memory_limit_mb":640}
    result=owner.post(f"/api/problems/{problem}/jobs",json={"kind":"verify","version":state["version"]})
    assert result.status_code==202 and result.json()["options"]["execution_backend"]=="zoj"


def test_stress_reference_preparation_is_not_reported_as_measured_judging(problem,tmp_path,monkeypatch):
    p=fixture_data(problem,tmp_path);p.job.kind="stress";p.result_lock=threading.RLock();p.stress_judge_budget=threading.BoundedSemaphore(1)
    p.blob=lambda content:hashlib.sha256(content).hexdigest()
    prepared=[]
    def prepare(*args,**kwargs):
        prepared.append(kwargs)
        return "AC",{"output":b"3\n","cpu_ms":9999,"memory_kb":99999}
    p.run_solution=prepare
    calls=[]
    def batch(*args):
        calls.append(args)
        return {"id":"judge1","node_name":"node1","agent_version":"0.2.27","runtime_revision":"real","status":"accepted",
                "cases":[{"verdict":"AC","cpu_ms":41,"wall_ms":None,"memory_kb":2048,"comment":"","actual_sha":"a"*64}]}
    monkeypatch.setattr(stress_judging,"run_batch",batch)
    monkeypatch.setattr(stress_judging,"read_blob",lambda _:b"3 \n")
    verdict,result=stress_judging.run(p,"main.cpp",b"1 2\n",None)
    assert verdict=="AC" and result["cpu_ms"]==41 and result["memory_kb"]==2048
    assert result["output"]==b"3\n" and result["actual_output"]==b"3 \n"
    assert prepared==[{"preparation":True}]
    assert calls[0][4][0]["answer_sha"]==hashlib.sha256(b"3\n").hexdigest()
    assert p.result["stress_judge_runs"][0]["solution"]=="main.cpp"


def test_stress_compile_error_is_preserved_without_invented_measurements(problem,tmp_path,monkeypatch):
    p=fixture_data(problem,tmp_path);p.job.kind="stress";p.result_lock=threading.RLock();p.stress_judge_budget=threading.BoundedSemaphore(1)
    p.blob=lambda content:hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(stress_judging,"run_batch",lambda *args:{"id":"ce1","node_name":"node1","agent_version":"0.2.27","runtime_revision":"real","status":"compile_error","compile_message":"syntax error","cases":[]})
    verdict,result=stress_judging.run(p,"main.cpp",b"1 2\n",b"3\n")
    assert verdict=="CE" and result["cpu_ms"] is None and result["memory_kb"] is None
    assert result["stderr"]=="syntax error" and len(p.result["stress_judge_runs"])==1


def test_stress_and_minimizer_never_run_the_editor_checker_after_real_judging():
    from app.stress_search import StressSearch
    from app.stress_minimizer import CounterexampleMinimizer as Minimizer
    calls=[]
    def checker(*args): calls.append(args);raise AssertionError("duplicate Editor checking")
    p=SimpleNamespace(run_solution=lambda *args:("AC",{"output":b"3\n","stderr":"", "judged_by_zoj":True}),checker=checker)
    search=object.__new__(StressSearch);search.p=p
    assert search.compare("main",b"1 2",b"3")[0]=="AC"
    minimizer=object.__new__(Minimizer);minimizer.p=p
    assert minimizer.verdict("main",b"1 2",b"3")[0]=="AC"
    assert calls==[]
