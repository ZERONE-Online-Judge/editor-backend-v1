import hashlib
import io
import json
import os
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

import pytest

from app.domain import references
from app.resources import compile_resources
from app.storage import read_blob


def package(properties=True, sections=True, prefix=""):
    content = io.BytesIO()
    files = {
        "problem.xml": b'''<problem><names><name language="korean" value="Imported sample"/></names>
        <statements><statement language="korean" type="application/x-tex" path="statements/korean/problem.tex"/></statements>
        <judging input-file="" output-file=""><testset name="tests"><time-limit>1000</time-limit><memory-limit>268435456</memory-limit>
        <input-path-pattern>tests/%02d</input-path-pattern><answer-path-pattern>tests/%02d.a</answer-path-pattern>
        <tests><test method="manual" sample="true" description="Example"/><test method="generated" cmd="gen 5 17" description="Random"/></tests></testset></judging>
        <files><resources><file path="files/testlib.h"/><file path="files/template.sty"/></resources>
        <executables><executable><source path="files/gen.cpp" type="cpp.gcc14-64-msys2-g++23"/></executable>
        <executable><source path="files/validate.cpp" type="cpp.g++17"/></executable></executables></files>
        <assets><checker name="std::ncmp.cpp"><source path="files/check.cpp"/></checker>
        <validators><validator><source path="files/validate.cpp" type="cpp.g++17"/></validator></validators>
        <solutions><solution tag="main"><source path="solutions/main.py" type="python.3"/></solution>
        <solution tag="wrong-answer"><source path="solutions/wrong.cpp" type="cpp.g++17"/></solution></solutions></assets>
        <stresses><list><stress path="stresses/001"/></list></stresses><tags><tag value="graphs"/></tags></problem>''',
        "files/testlib.h": b"original package header\n",
        "files/template.sty": b"original style\n",
        "files/gen.cpp": b"int main() { return 0; }\n",
        "files/validate.cpp": b"int main() { return 0; }\n",
        "files/check.cpp": b"original standard checker\n",
        "solutions/main.py": b"print(42)\n",
        "solutions/wrong.cpp": b"int main() { return 1; }\n",
        "solutions/wrong.cpp.desc": b"Tag: WRONG_ANSWER\n",
        "tests/01": b"2\r\n",
        "tests/01.a": b"42\r\n",
        "tests/02": b"5\n",
        "tests/02.a": b"42\n",
        "stresses/001": b'{"scriptPattern":"gen [1..10] {seed}","solutionNames":["wrong.cpp"]}',
        "statements/korean/problem.tex": br'''\documentclass{article}
\begin{document}\begin{problem}{Imported {sample}}{standard input}{standard output}{1 second}{256 megabytes}
Find $N$.
\InputFile
Input body.
\OutputFile
Print \texttt{-1}.
\Examples
\begin{example}\exmpfile{example.01}{example.01.a}\end{example}
\Note
Note body.
\end{problem}\end{document}''',
        "statements/korean/example.01.mu": b"\x00\xf3binary display data",
    }
    statement = {"name": "Imported sample", "legend": "Find $N$.", "input": "Input body.", "output": r"Print \texttt{-1}.", "notes": "Note body."}
    if properties:
        files["statements/korean/problem-properties.json"] = json.dumps({**statement, "scoring": None, "interaction": None, "tutorial": "", "authorLogin": "fixture", "sampleTests": [{"input": "2\n", "output": "42\n"}]}).encode()
    if sections:
        for key, value in statement.items():
            files[f"statement-sections/korean/{key}.tex"] = value.encode()
        files["statement-sections/korean/example.01"] = b"2\n"
        files["statement-sections/korean/example.01.a"] = b"42\n"
    with zipfile.ZipFile(content, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(prefix + name, value)
    return content.getvalue(), files


def imported(owner, archive):
    response = owner.post("/api/imports?name=Imported&slug=imported-package", content=archive)
    assert response.status_code == 201, response.text
    path = "/api/problems/" + response.json()["id"]
    document = owner.get(path).json()["document"]
    assert owner.get(path + "/revisions/0").json() == document
    return document, response.json()["import_report"]


@pytest.mark.parametrize("properties,sections,prefix", [(True, True, ""), (False, True, "wrapped/"), (False, False, "")])
def test_official_polygon_topology_and_statement_sources(owner, properties, sections, prefix):
    archive, _ = package(properties, sections, prefix)
    doc, report = imported(owner, archive)
    assert doc["solutions"]["main.py"]["tag"] == "MA"
    assert doc["solutions"]["wrong.cpp"]["tag"] == "WA"
    assert doc["validator"] == "validate.cpp"
    assert doc["files"]["validate.cpp"]["roles"] == ["validator"]
    assert doc["files"]["gen.cpp"]["roles"] == ["generator"]
    assert doc["files"]["gen.cpp"]["language"] == "cpp20"
    assert doc["checker"] == "std::ncmp" and "testlib.h" not in doc["files"]
    assert doc["info"]["tags"] == ["graphs"]
    assert (doc["info"]["input_file"], doc["info"]["output_file"]) == ("stdin", "stdout")
    statement = doc["statements"]["korean"]
    assert statement["legend"] == "Find $N$."
    assert statement["input"] == "Input body."
    assert statement["output"] == r"Print \texttt{-1}."
    assert statement["notes"] == "Note body."
    assert "\\begin{problem}" not in json.dumps(statement)
    tests = doc["testsets"]["tests"]["tests"]
    assert tests["2"]["command"] == "gen 5 17"
    assert tests["2"]["description"] == "Random"
    assert read_blob(tests["1"]["input_sha"]) == b"2\r\n"
    if properties or sections:
        assert tests["1"]["statement_input"] == "2\n"
        assert tests["1"]["statement_output"] == "42\n"
    assert any("C++20" in message for message in report["warnings"])
    assert report["solutions"] == 2 and report["tests"] == 2


def test_every_original_payload_is_retained_without_attaching_aux_to_execution(owner):
    archive, originals = package()
    doc, report = imported(owner, archive)
    checksums = references(doc)
    for name, value in originals.items():
        sha = hashlib.sha256(value).hexdigest()
        assert sha in checksums, name
        assert read_blob(sha) == value, name
    aliases = {row["path"]: doc["files"][row["name"]] for row in report["preserved_files"]}
    for name in ("files/testlib.h", "files/check.cpp", "stresses/001", "solutions/wrong.cpp.desc", "statements/korean/example.01.mu"):
        assert aliases[name]["kind"] == "aux" and aliases[name]["attach_to_build"] is False
    attached = compile_resources(list(doc["files"].values()), "cpp20", True)
    assert not set(row["name"] for row in aliases.values()).intersection(row["name"] for row in attached)
    assert "template.sty" in {row["name"] for row in attached}


def test_duplicate_flattened_names_fail_instead_of_overwriting_sources(owner):
    _, originals = package()
    originals["problem.xml"] = originals["problem.xml"].replace(b"</executables>", b'<executable><source path="other/gen.cpp"/></executable></executables>')
    originals["other/gen.cpp"] = b"different source"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in originals.items():
            archive.writestr(name, value)
    response = owner.post("/api/imports?name=Imported&slug=collision", content=output.getvalue())
    assert response.status_code == 400, response.text
    assert "Duplicate package filename" in response.text
    assert owner.get("/api/problems").json() == []


def test_unmapped_files_respect_the_document_limit_without_silently_dropping_originals(owner):
    raw, _ = package()
    output = io.BytesIO(raw)
    with zipfile.ZipFile(output, "a") as archive:
        for index in range(200):
            archive.writestr(f"extras/{index}.txt", b"original")
    response = owner.post("/api/imports?name=Imported&slug=many-originals", content=output.getvalue())
    assert response.status_code == 400, response.text
    assert "200-file limit" in response.text
    assert owner.get("/api/problems").json() == []


def test_legacy_editor_xml_scopes_resources_and_component_tests_survive(owner):
    from types import SimpleNamespace
    from app.packages import descriptor
    from app.schema import empty_document
    doc = empty_document("Legacy")
    source = {"name": "main.py", "kind": "source", "language": "python3", "tag": "MA", "sha256": "a" * 64, "size": 10, "extra_tags": {}}
    doc["solutions"] = {"main.py": source}
    doc["solutions"]["wrong.py"] = {**source, "name": "wrong.py", "tag": "WA", "extra_tags": {"tests": "WA"}}
    doc["statements"]["korean"]["legend"] = "Legacy $N$."
    doc["files"] = {"lib.txt": {"name": "lib.txt", "kind": "aux", "language": "text", "attach_to_build": False, "sha256": "b" * 64, "size": 3}}
    doc["files"]["validator.py"] = {"name": "validator.py", "kind": "source", "language": "python3", "sha256": "c" * 64, "size": 10}
    doc["validator"] = "validator.py"
    doc["validator_tests"] = {"1": {"input_sha": "d" * 64, "output_sha": None, "answer_sha": None, "expected": "VALID"}}
    doc["checker_tests"] = {"1": {"input_sha": "d" * 64, "output_sha": "e" * 64, "answer_sha": "e" * 64, "expected": "OK"}}
    prepared = {"tests": [{"index": 1, "sample": False, "command": "", "group": ""}]}
    xml = descriptor(SimpleNamespace(document=doc, revision=1, problem_id="fixture"), prepared)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("problem.xml", xml)
        archive.writestr("solutions/main.py", "print(42)\n")
        archive.writestr("solutions/wrong.py", "print(0)\n")
        archive.writestr("files/lib.txt", "lib")
        archive.writestr("files/validator.py", "print(42)\n")
        archive.writestr("tests/tests/001", "1\n")
        archive.writestr("tests/tests/001.a", "42\n")
        archive.writestr("statements/korean/properties.json", json.dumps(doc["statements"]["korean"]))
        archive.writestr("validator-tests/001", "1\n")
        archive.writestr("checker-tests/001", "1\n")
        archive.writestr("checker-tests/001.o", "42\n")
        archive.writestr("checker-tests/001.a", "42\n")
    restored, _ = imported(owner, output.getvalue())
    assert restored["files"]["lib.txt"]["attach_to_build"] is False
    assert restored["statements"]["korean"] == doc["statements"]["korean"]
    assert restored["solutions"]["main.py"]["tag"] == "MA"
    assert restored["solutions"]["wrong.py"]["extra_tags"] == {"tests": "WA"}
    assert restored["validator_tests"]["1"]["expected"] == "VALID"
    assert restored["checker_tests"]["1"]["expected"] == "OK"
    assert read_blob(restored["checker_tests"]["1"]["answer_sha"]) == b"42\n"


@pytest.mark.skipif(not os.environ.get("EDITOR_REAL_POLYGON_PACKAGE"), reason="Private package supplied only for local verification")
def test_real_package_has_no_missing_payloads_or_authoring_sections(owner):
    raw = Path(os.environ["EDITOR_REAL_POLYGON_PACKAGE"]).read_bytes()
    doc, report = imported(owner, raw)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        root = ET.fromstring(archive.read("problem.xml"))
        expected = {PurePosixPath(node.get("path")).name: node.get("path") for node in root.findall("assets/solutions/solution/source")}
        assert set(doc["solutions"]) == set(expected)
        for name, path in expected.items():
            assert read_blob(doc["solutions"][name]["sha256"]) == archive.read(path)
        for source in root.findall("files/executables/executable/source"):
            path = source.get("path")
            assert read_blob(doc["files"][PurePosixPath(path).name]["sha256"]) == archive.read(path)
        for source in root.findall("assets/validators/validator/source"):
            assert PurePosixPath(source.get("path")).name in [doc["validator"], *doc["extra_validators"]]
        properties = json.loads(archive.read("statements/korean/problem-properties.json"))
        for key in ("legend", "input", "output", "notes", "tutorial"):
            assert doc["statements"]["korean"][key] == (properties.get(key) or "")
        windows_text = any(n.get("type") == "exe.win32" for n in root.findall(".//binary")) and any(n.get("name") == "tests-wellformed" and n.get("value") == "true" for n in root.findall("properties/property"))
        def portable(value):
            return value.replace(b"\r\n", b"\n") if windows_text else value
        tests = doc["testsets"]["tests"]["tests"]
        nodes = root.findall("judging/testset/tests/test")
        assert len(tests) == len(nodes)
        for index, node in enumerate(nodes, 1):
            assert tests[str(index)]["command"] == node.get("cmd", "")
            assert tests[str(index)]["description"] == node.get("description", "")
            test = tests[str(index)]
            input_path = "tests/%02d" % index
            if input_path in archive.namelist():
                assert read_blob(test["input_sha"]) == portable(archive.read(input_path))
            else:
                assert node.get("method") == "generated" and node.get("cmd", "").strip()
                assert test["input_sha"] is None
            answer_path = input_path + ".a"
            if answer_path in archive.namelist():
                assert read_blob(test["answer_sha"]) == portable(archive.read(answer_path))
            else:
                assert test["answer_sha"] is None
        for index, sample in enumerate(properties["sampleTests"], 1):
            assert tests[str(index)]["statement_input"] == (sample["input"].replace("\r\n", "\n") if windows_text else sample["input"])
            assert tests[str(index)]["statement_output"] == (sample["output"].replace("\r\n", "\n") if windows_text else sample["output"])
        checksums = references(doc)
        for entry in archive.infolist():
            if not entry.is_dir():
                raw_file = archive.read(entry.filename)
                sha = hashlib.sha256(raw_file).hexdigest()
                assert sha in checksums, entry.filename
                assert read_blob(sha) == raw_file
    assert report["solutions"] == len(expected)
    destination = os.environ.get("EDITOR_POLYGON_IMPORT_REPORT_DIR")
    if destination:
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "after.json").write_text(json.dumps({"archive_sha256": hashlib.sha256(raw).hexdigest(),
            "solution_count": len(doc["solutions"]), "validator": doc["validator"], "checker": doc["checker"],
            "test_count": sum(len(ts["tests"]) for ts in doc["testsets"].values()),
            "all_original_payloads_preserved": True, "import_report": report}, ensure_ascii=False, indent=2))
        (folder / "render-input.json").write_text(json.dumps(doc["statements"], ensure_ascii=False))
        (folder / "render-samples.json").write_text(json.dumps([
            {"input": test.get("statement_input") if test.get("statement_input") is not None else read_blob(test["input_sha"]).decode(),
             "output": test.get("statement_output") if test.get("statement_output") is not None else read_blob(test["answer_sha"]).decode()}
            for ts in doc["testsets"].values() for test in ts["tests"].values() if test["sample"]], ensure_ascii=False))


@pytest.mark.sandbox
@pytest.mark.skipif(not os.environ.get("EDITOR_REAL_POLYGON_PACKAGE") or os.environ.get("RUN_SANDBOX_TESTS") != "1", reason="Opt-in private-package sandbox verification")
def test_real_imported_programs_compile_and_examples_validate_in_sandbox(owner, queue_legacy_job):
    from types import SimpleNamespace
    from app.pipeline import Pipeline
    raw = Path(os.environ["EDITOR_REAL_POLYGON_PACKAGE"]).read_bytes()
    doc, _ = imported(owner, raw)
    response = owner.get("/api/problems").json()
    problem_id = response[0]["id"]
    from app.db import Problem, SessionLocal
    with SessionLocal() as db:
        user_id = db.get(Problem, problem_id).owner_id
    job = SimpleNamespace(id="polygon-import-check", kind="verify", options={}, document=doc, problem_id=problem_id, user_id=user_id)
    pipeline = Pipeline(job, lambda _: None, lambda: None)
    main = next(name for name, source in doc["solutions"].items() if source["tag"] == "MA")
    results = []
    try:
        for name in doc["solutions"]:
            pipeline.program(name, solution=True)
        for name, source in doc["files"].items():
            if source["kind"] == "source":
                pipeline.program(name)
        for test in doc["testsets"]["tests"]["tests"].values():
            if not test["sample"]:
                continue
            inp = read_blob(test["input_sha"])
            valid, result = pipeline.validator(inp)
            assert valid, result
            verdict, result = pipeline.run_solution(main, inp)
            assert verdict == "AC", result
            answer = read_blob(test["answer_sha"]) if test["answer_sha"] else result["output"]
            verdict, result = pipeline.checker(inp, result["output"], answer)
            assert verdict == "AC", result
            from app.statement_samples import validate
            assert validate(pipeline, test, "tests", inp, answer, main) == "VALID"
            results.append({"index": test["index"], "validator": "VALID", "main": verdict, "statement_io": "VALID"})
        generated_count = 0
        for test in doc["testsets"]["tests"]["tests"].values():
            if not test["command"]:
                continue
            generated = pipeline.generator(test["command"])
            valid, result = pipeline.validator(generated)
            assert valid, result
            generated_count += 1
        pipeline.unit_tests("validator-tests")
        assert all(case["matched"] for case in pipeline.result["unit_tests"]), pipeline.result["unit_tests"]
        artifact = os.environ.get("EDITOR_POLYGON_IMPORT_REPORT")
        if artifact:
            Path(artifact).write_text(json.dumps({"solutions_compiled": len(doc["solutions"]),
                "sources_compiled": sum(source["kind"] == "source" for source in doc["files"].values()),
                "registered_examples": results, "generator_output_validator": "VALID", "generated_tests_validated": generated_count, "validator_self_tests": pipeline.result["unit_tests"]}, indent=2))
    finally:
        pipeline.sandbox.close()


def rewrite_package(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return stream.getvalue()


@pytest.mark.parametrize('prefix', ['', 'wrapped/'])
def test_source_package_preserves_pending_generated_tests_without_inventing_data(owner, prefix):
    _, files = package()
    del files['tests/02']
    del files['tests/01.a']
    del files['tests/02.a']
    # The component-test file with the same basename is unrelated data.
    files['files/tests/validator-tests/02'] = b'unrelated validator fixture\n'
    doc, report = imported(owner, rewrite_package({prefix+name: data for name,data in files.items()}))
    manual, generated = doc['testsets']['tests']['tests'].values()
    assert read_blob(manual['input_sha']) == b'2\r\n'
    assert manual['answer_sha'] is None
    assert generated['input_sha'] is None and generated['answer_sha'] is None
    assert generated['command'] == 'gen 5 17'
    assert generated['description'] == 'Random'
    assert doc['files']['gen.cpp']['roles'] == ['generator']
    assert manual['statement_input'] == '2\n' and manual['statement_output'] == '42\n'
    assert any('아직 생성되지 않은 입력 1개' in warning for warning in report['warnings'])
    assert any('정답 파일이 없는 테스트 2개' in warning for warning in report['warnings'])
    assert report['tests'] == 2


@pytest.mark.parametrize('method, command', [('manual', None), ('manual', 'gen 5 17'), ('generated', None), ('generated', '   ')])
def test_missing_input_is_an_error_without_a_declared_generator_command(owner, method, command):
    _, files = package()
    del files['tests/02']
    root = ET.fromstring(files['problem.xml'])
    test = root.findall('judging/testset/tests/test')[1]
    test.set('method', method)
    test.attrib.pop('cmd', None)
    if command is not None:
        test.set('cmd', command)
    files['problem.xml'] = ET.tostring(root)
    response = owner.post('/api/imports?name=Broken&slug=broken-package', content=rewrite_package(files))
    assert response.status_code == 400
    assert 'Missing package file: tests/02' in response.json()['error']['message']
    assert owner.get('/api/problems').json() == []


def test_wellformed_windows_tests_use_linux_newlines_and_keep_exact_originals(owner):
    _, files = package()
    root = ET.fromstring(files['problem.xml'])
    ET.SubElement(root.find('assets/solutions/solution'), 'binary', path='solutions/main.exe', type='exe.win32')
    ET.SubElement(ET.SubElement(root, 'properties'), 'property', name='tests-wellformed', value='true')
    validator_tests = ET.SubElement(root.find('assets/validators/validator'), 'testset')
    ET.SubElement(validator_tests, 'input-path-pattern').text = 'validator-tests/%02d'
    ET.SubElement(ET.SubElement(validator_tests, 'tests'), 'test', verdict='valid')
    files['validator-tests/01'] = b'2\r\n'
    files['problem.xml'] = ET.tostring(root)
    properties = json.loads(files['statements/korean/problem-properties.json'])
    properties['sampleTests'] = [{'input':'2\r\n','output':'42\r\n'}]
    files['statements/korean/problem-properties.json'] = json.dumps(properties).encode()
    doc, report = imported(owner, rewrite_package(files))
    test = doc['testsets']['tests']['tests']['1']
    assert read_blob(test['input_sha']) == b'2\n'
    assert read_blob(test['answer_sha']) == b'42\n'
    assert read_blob(doc['validator_tests']['1']['input_sha']) == b'2\n'
    assert test['statement_input'] == '2\n' and test['statement_output'] == '42\n'
    originals = {entry['path']: entry['name'] for entry in report['preserved_files']}
    for path in ['tests/01','tests/01.a','validator-tests/01']:
        source = doc['files'][originals[path]]
        assert source['kind'] == 'aux' and source['attach_to_build'] is False
        assert read_blob(source['sha256']) == files[path]
    assert any('Linux LF' in warning for warning in report['warnings'])


@pytest.mark.parametrize('content', [b'\0\r\n', b'\xff\r\n', b'bare\rreturn\n'])
def test_windows_import_does_not_rewrite_binary_or_other_line_endings(owner, content):
    _, files = package()
    root = ET.fromstring(files['problem.xml'])
    ET.SubElement(root.find('assets/solutions/solution'), 'binary', type='exe.win32')
    ET.SubElement(ET.SubElement(root, 'properties'), 'property', name='tests-wellformed', value='true')
    files['problem.xml'] = ET.tostring(root)
    files['tests/02'] = content
    doc, _ = imported(owner, rewrite_package(files))
    assert read_blob(doc['testsets']['tests']['tests']['2']['input_sha']) == content
