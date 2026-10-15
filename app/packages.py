import copy
import hashlib
import html
import io
import json
import re
import stat
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath
from fastapi import APIRouter, Depends, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth import current_user
from .config import get_settings
from .db import Access, Blob, Collection, Job, Problem, Revision, User, now, session_dependency
from .domain import authorize, fail, references, validate_document
from .schema import Document, Strict, empty_document, safe_name
from .storage import put_blob, read_blob
from .sandbox import VENDOR
from .folders import owned_folder, place

router = APIRouter(prefix="/api", tags=["packages"])
SOURCE_TYPES = {"cpp17": "cpp.g++17", "cpp20": "cpp.g++20", "c17": "c.gcc", "c99": "c.gcc", "python3": "python.3", "java17": "java.17", "java8": "java.8", "text": "text"}
TAG_NAMES = {"MA": "main", "OK": "accepted", "WA": "wrong-answer", "TL": "time-limit-exceeded", "ML": "memory-limit-exceeded", "PE": "presentation-error", "RE": "runtime-error", "RJ": "rejected", "TO": "time-limit-exceeded-or-accepted", "TM": "time-limit-exceeded-or-memory-limit-exceeded", "NR": "do-not-run"}


def statement_html(doc, language, assets_prefix="../../assets"):
    statement = doc["statements"][language]
    title = html.escape(statement["name"] or doc["info"]["name"])
    sections = [("문제", "legend"), ("입력", "input"), ("출력", "output"), ("부분 점수", "scoring"), ("인터랙션", "interaction"), ("노트", "notes"), ("해설", "tutorial")]
    body = "".join(f"<section><h2>{label}</h2><div class='copy'>{html.escape(statement[key])}</div></section>" for label, key in sections if statement[key])
    return f"<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>{title}</title><link rel='stylesheet' href='{assets_prefix}/katex/katex.min.css'><style>body{{font:16px/1.8 sans-serif;max-width:900px;margin:40px auto;padding:0 24px;color:#0f172a}}h1{{text-align:center}}h2{{font-size:18px}}.copy{{white-space:pre-wrap}}pre{{padding:16px;background:#f7f8fd}}</style></head><body><h1>{title}</h1><p>시간 제한 {doc['info']['time_limit_ms']}ms · 메모리 {doc['info']['memory_limit_mb']}MiB</p>{body}<script src='{assets_prefix}/katex/katex.min.js'></script><script src='{assets_prefix}/katex/auto-render.min.js'></script><script>renderMathInElement(document.body,{{delimiters:[{{left:'$$',right:'$$',display:true}},{{left:'$',right:'$',display:false}},{{left:'\\\\[',right:'\\\\]',display:true}},{{left:'\\\\(',right:'\\\\)',display:false}}],throwOnError:false}})</script></body></html>"


def descriptor(job, testsets):
    doc = job.document
    root = ET.Element("problem", {"short-name": doc["info"]["name"], "revision": str(job.revision), "url": f"https://editor.zoj.kr/problems/{job.problem_id}"})
    names = ET.SubElement(root, "names")
    for lang, statement in doc["statements"].items():
        ET.SubElement(names, "name", {"language": lang, "value": statement["name"] or doc["info"]["name"]})
    statements = ET.SubElement(root, "statements")
    for lang in doc["statements"]:
        ET.SubElement(statements, "statement", {"language": lang, "type": "text/html", "path": f"statements/{lang}/statement.html"})
        ET.SubElement(statements, "statement", {"language": lang, "type": "application/x-tex", "path": f"statements/{lang}/statement.tex"})
    judging = ET.SubElement(root, "judging", {"input-file": doc["info"]["input_file"], "output-file": doc["info"]["output_file"]})
    for set_name, tests in testsets.items():
        ts = ET.SubElement(judging, "testset", {"name": set_name})
        for key, value in {"time-limit": doc["info"]["time_limit_ms"], "memory-limit": doc["info"]["memory_limit_mb"] * 1048576, "test-count": len(tests), "input-path-pattern": f"tests/{set_name}/%03d", "answer-path-pattern": f"tests/{set_name}/%03d.a"}.items():
            ET.SubElement(ts, key).text = str(value)
        items = ET.SubElement(ts, "tests")
        for test in tests:
            attrs = {"method": "generated" if test.get("command") else "manual", "sample": str(test["sample"]).lower()}
            if test.get("command"):
                attrs["cmd"] = test["command"]
            if test.get("group"):
                attrs["group"] = test["group"]
            if doc["info"]["points_enabled"]:
                attrs["points"] = str(test.get("points", 0))
            ET.SubElement(items, "test", attrs)
        groups = ET.SubElement(ts, "groups")
        for name, group in doc["testsets"][set_name]["groups"].items():
            ET.SubElement(groups, "group", {"name": name, "points-policy": group["points_policy"].lower().replace("_", "-"), "feedback-policy": group["feedback_policy"].lower(), "dependencies": ",".join(group["dependencies"])})
    solutions = ET.SubElement(root, "solutions")
    for name, source in doc["solutions"].items():
        s = ET.SubElement(solutions, "solution", {"tag": TAG_NAMES[source["tag"]]})
        ET.SubElement(s, "source", {"path": f"solutions/{name}", "type": SOURCE_TYPES[source["language"]]})
        # Editor extension; do not pretend a private descriptor is Polygon syntax.
        extra = ET.SubElement(s, "{https://editor.zoj.kr/schema/v1}expectations")
        for scope, tag in source.get("extra_tags", {}).items():
            ET.SubElement(extra, "{https://editor.zoj.kr/schema/v1}scope", {"testset": scope, "tag": tag})
        for set_name, groups in source.get("group_tags", {}).items():
            for group, tag in groups.items():
                ET.SubElement(extra, "{https://editor.zoj.kr/schema/v1}scope", {"testset": set_name, "group": group, "tag": tag})
    files = ET.SubElement(root, "files")
    for kind in ["source", "resource", "aux", "statement"]:
        category = ET.SubElement(files, {"source": "sources", "resource": "resources", "aux": "aux", "statement": "statement-resources"}[kind])
        for name, source in doc["files"].items():
            if source["kind"] == kind:
                ET.SubElement(category, "file", {"path": f"files/{name}", "type": SOURCE_TYPES[source["language"]]})
    assets = ET.SubElement(root, "assets")
    for role in ["checker", "validator", "interactor"]:
        name = doc[role]
        if not name:
            continue
        is_standard = name.startswith("std::")
        file_name = "std-" + name.removeprefix("std::") + ".cpp" if is_standard else name
        attrs = {"name": name + ".cpp" if is_standard else name}
        if role == "checker":
            attrs["type"] = "testlib"
        asset = ET.SubElement(assets, role, attrs)
        source = doc["files"].get(name)
        ET.SubElement(asset, "source", {"path": f"files/{file_name}", "type": SOURCE_TYPES[source["language"]] if source else "cpp.g++17"})
        units = doc["validator_tests" if role == "validator" else "checker_tests"] if role != "interactor" else {}
        if units:
            testset = ET.SubElement(asset, "testset")
            ET.SubElement(testset, "test-count").text = str(len(units))
            ET.SubElement(testset, "input-path-pattern").text = f"{role}-tests/%03d"
            if role == "checker":
                ET.SubElement(testset, "output-path-pattern").text = "checker-tests/%03d.o"
                ET.SubElement(testset, "answer-path-pattern").text = "checker-tests/%03d.a"
            tests = ET.SubElement(testset, "tests")
            for unit in units.values():
                ET.SubElement(tests, "test", {"verdict": unit["expected"].lower().replace("_", "-")})
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_package(job, testsets):
    if job.options.get("export_format") == "zoj":
        from .zoj_package import build_zoj_package
        return build_zoj_package(job, testsets)
    doc = copy.deepcopy(job.document)
    for name, tests in testsets.items():
        doc["testsets"][name]["tests"] = {str(t["index"]): {key: value for key, value in t.items() if key in {"index", "input_sha", "answer_sha", "command", "group", "points", "description", "sample"}} for t in tests}
        doc["testsets"][name]["script"] = ""
    destination = get_settings().data_dir / "packages" / (job.id + ".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("editor.json", json.dumps({"format": "zoj-editor-v1", "revision": job.revision, "source_document_hash": job.document_hash, "document": doc}, ensure_ascii=False))
        archive.writestr("problem.xml", descriptor(job, testsets))
        for sha in references(doc):
            archive.writestr("blobs/" + sha, read_blob(sha))
        for source in [*doc["files"].values(), *doc["solutions"].values()]:
            prefix = "solutions" if source["name"] in doc["solutions"] and doc["solutions"][source["name"]] == source else "files"
            archive.writestr(f"{prefix}/{source['name']}", read_blob(source["sha256"]))
        for set_name, tests in testsets.items():
            for ordinal, test in enumerate(tests, 1):
                prefix = f"tests/{set_name}/{ordinal:03d}"
                archive.writestr(prefix, read_blob(test["input_sha"]))
                archive.writestr(prefix + ".a", read_blob(test["answer_sha"]))
        archive.writestr("files/testlib.h", (VENDOR / "testlib.h").read_bytes())
        archive.writestr("files/TESTLIB-LICENSE", (VENDOR / "LICENSE").read_bytes())
        if doc["checker"].startswith("std::"):
            name = doc["checker"].removeprefix("std::") + ".cpp"
            archive.writestr("files/std-" + name, (VENDOR / name).read_bytes())
        for kind in ["validator", "checker"]:
            for index, test in enumerate(doc[kind + "_tests"].values(), 1):
                archive.writestr(f"{kind}-tests/{index:03d}", read_blob(test["input_sha"]))
                for key, suffix in [("output_sha", ".o"), ("answer_sha", ".a")]:
                    if test.get(key):
                        archive.writestr(f"{kind}-tests/{index:03d}{suffix}", read_blob(test[key]))
        from .rendering import statement_tex
        for lang, statement in doc["statements"].items():
            archive.writestr(f"statements/{lang}/statement.html", statement_html(doc, lang))
            archive.writestr(f"statements/{lang}/statement.tex", statement_tex(doc, lang))
            archive.writestr(f"statements/{lang}/properties.json", json.dumps(statement, ensure_ascii=False))
        for name, material in doc["materials"].items():
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as zip_material:
                for fname in material["files"]:
                    zip_material.writestr(fname, read_blob(doc["files"][fname]["sha256"]))
                for fname in material["solutions"]:
                    zip_material.writestr(fname, read_blob(doc["solutions"][fname]["sha256"]))
                for set_name in material["testsets"]:
                    for test in testsets[set_name]:
                        zip_material.writestr(f"tests/{set_name}/{test['index']:03d}", read_blob(test["input_sha"]))
                        zip_material.writestr(f"tests/{set_name}/{test['index']:03d}.a", read_blob(test["answer_sha"]))
            archive.writestr(f"materials/{name}.zip", payload.getvalue())
        katex = Path(__file__).resolve().parent.parent / "vendor" / "katex"
        if katex.exists():
            for path in katex.rglob("*"):
                if path.is_file():
                    archive.write(path, "assets/katex/" + str(path.relative_to(katex)))
        archive.writestr("README.txt", "ZOJ Editor problem package\nSources and generated tests; no compiled binaries.\nCompile source files with their declared language.\nAll platform testlib sources retain the upstream MIT license.\n")
    return {"filename": job.problem_id[:8] + f"-r{job.revision}.zip", "format": "editor", "size": destination.stat().st_size, "verified": bool(job.options["verify"]), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


def checked_archive(content):
    settings = get_settings()
    if len(content) > settings.max_archive_bytes:
        fail("too_large", "압축 파일은 128MiB 이하여야 합니다.", 413)
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
        names = set()
        total = 0
        if len(archive.infolist()) > 15000:
            raise ValueError("Too many archive entries")
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in entry.filename or entry.filename in names or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Unsafe archive path")
            names.add(entry.filename)
            if entry.file_size > settings.max_upload_bytes:
                raise ValueError("An archive entry exceeds 16MiB")
            total += entry.file_size
            if total > settings.max_uncompressed_archive_bytes:
                raise ValueError("Uncompressed archive limit exceeded")
        return archive
    except (zipfile.BadZipFile, ValueError) as error:
        fail("invalid_archive", f"문제 패키지를 확인해 주세요: {error}")


def guess_language(name, source_type=""):
    if source_type == "java.8":
        return "java8"
    if "java" in source_type or name.endswith(".java"):
        return "java17"
    if "python" in source_type or name.endswith(".py"):
        return "python3"
    if "20" in source_type:
        return "cpp20"
    if name.endswith(".c"):
        return "c17"
    if name.endswith((".cpp", ".cc", ".cxx")):
        return "cpp17"
    return "text"


def pattern_path(pattern, index):
    try:
        return pattern % index
    except (TypeError, ValueError):
        raise ValueError("Unsupported test path pattern")


def polygon_document(archive, db, problem_id, name):
    descriptor_path = next((n for n in archive.namelist() if PurePosixPath(n).name == "problem.xml"), None)
    if not descriptor_path:
        raise ValueError("editor.json or problem.xml is required")
    prefix = str(PurePosixPath(descriptor_path).parent)
    prefix = "" if prefix == "." else prefix + "/"
    xml_bytes = archive.read(descriptor_path)
    if len(xml_bytes) > 1048576 or re.search(br"<!\s*(DOCTYPE|ENTITY)", xml_bytes, re.I):
        raise ValueError("Unsafe XML descriptor")
    root = ET.fromstring(xml_bytes)
    doc = empty_document(name)
    judging = root.find("judging")
    if judging is None:
        raise ValueError("No judging descriptor")
    doc["info"]["input_file"] = judging.get("input-file", "stdin") or "stdin"
    doc["info"]["output_file"] = judging.get("output-file", "stdout") or "stdout"
    doc["info"]["interactive"] = root.find("assets/interactor") is not None
    doc["info"]["well_formed"] = False
    def read(path):
        target = prefix + path
        if target not in archive.namelist():
            raise ValueError(f"Missing package file: {path}")
        return archive.read(target)
    def file(path, kind="source", source_type=""):
        fname = safe_name(PurePosixPath(path).name)
        if fname == "testlib.h":
            return None
        content = read(path)
        blob = put_blob(db, problem_id, content)
        return {"name": fname, "kind": kind, "language": guess_language(fname, source_type), **blob}
    doc["files"] = {}
    for category, kind in [("sources", "source"), ("resources", "resource"), ("aux", "aux"), ("statement-resources", "statement")]:
        for node in root.findall(f"files/{category}/file"):
            item = file(node.get("path"), kind, node.get("type", ""))
            if item:
                doc["files"][item["name"]] = item
    doc["solutions"] = {}
    reverse_tags = {value: key for key, value in TAG_NAMES.items()}
    for node in root.findall("solutions/solution"):
        src = node.find("source")
        if src is None:
            continue
        item = file(src.get("path"), "source", src.get("type", ""))
        item.update(tag=reverse_tags.get(node.get("tag"), node.get("tag", "OK")), extra_tags={})
        for scope in node.findall("{https://editor.zoj.kr/schema/v1}expectations/{https://editor.zoj.kr/schema/v1}scope"):
            set_name, group, tag = scope.get("testset"), scope.get("group"), scope.get("tag")
            if group:
                item.setdefault("group_tags", {}).setdefault(set_name, {})[group] = tag
            else:
                item["extra_tags"][set_name] = tag
        doc["solutions"][item["name"]] = item
    for role in ["checker", "validator", "interactor"]:
        asset = root.find("assets/" + role)
        if asset is None:
            doc[role] = ""
            continue
        standard = asset.get("name", "")
        if role == "checker" and standard.startswith("std::"):
            doc[role] = standard.removesuffix(".cpp")
            continue
        source = asset.find("source")
        if source is not None:
            item = file(source.get("path"), "source", source.get("type", ""))
            doc["files"][item["name"]] = item
            doc[role] = item["name"]
        tests = asset.find("testset")
        if tests is not None and role in {"checker", "validator"}:
            for index, node in enumerate(tests.findall("tests/test"), 1):
                unit = {"input_sha": put_blob(db, problem_id, read(pattern_path(tests.findtext("input-path-pattern"), index)))["sha256"], "output_sha": None, "answer_sha": None, "expected": node.get("verdict").upper().replace("-", "_")}
                for field, xml_key in [("output_sha", "output-path-pattern"), ("answer_sha", "answer-path-pattern")]:
                    pattern = tests.findtext(xml_key)
                    if pattern:
                        unit[field] = put_blob(db, problem_id, read(pattern_path(pattern, index)))["sha256"]
                doc[role + "_tests"][str(index)] = unit
    doc["testsets"] = {}
    for testset in judging.findall("testset"):
        set_name = safe_name(testset.get("name", "tests"))
        doc["info"]["time_limit_ms"] = int(testset.findtext("time-limit", "1000"))
        doc["info"]["memory_limit_mb"] = int(testset.findtext("memory-limit", "268435456")) // 1048576
        tests, groups = {}, {}
        for index, test in enumerate(testset.findall("tests/test"), 1):
            inp = put_blob(db, problem_id, read(pattern_path(testset.findtext("input-path-pattern"), index)))["sha256"]
            answer_pattern = testset.findtext("answer-path-pattern")
            answer_path = pattern_path(answer_pattern, index) if answer_pattern else None
            answer = put_blob(db, problem_id, read(answer_path))["sha256"] if answer_path and prefix + answer_path in archive.namelist() else None
            if float(test.get("points", "0")) > 0:
                doc["info"]["points_enabled"] = True
            group = test.get("group", "")
            if group:
                groups[group] = {"points_policy": "COMPLETE_GROUP", "feedback_policy": "COMPLETE", "dependencies": []}
            tests[str(index)] = {"index": index, "input_sha": inp, "answer_sha": answer, "command": "", "group": group, "points": float(test.get("points", "0")), "description": "", "sample": test.get("sample") == "true"}
        for group in testset.findall("groups/group"):
            groups[group.get("name")] = {"points_policy": group.get("points-policy", "complete-group").upper().replace("-", "_"), "feedback_policy": group.get("feedback-policy", "complete").upper(), "dependencies": [d for d in group.get("dependencies", "").split(",") if d]}
        doc["testsets"][set_name] = {"script": "", "tests": tests, "groups_enabled": bool(groups), "groups": groups}
    doc["statements"] = {}
    for lang_node in root.findall("names/name"):
        language = safe_name(lang_node.get("language", "english"))
        props_name = prefix + f"statements/{language}/properties.json"
        if props_name in archive.namelist():
            doc["statements"][language] = json.loads(archive.read(props_name))
        else:
            nodes = [s for s in root.findall("statements/statement") if s.get("language") == language]
            tex_node = next((s for s in nodes if s.get("type") == "application/x-tex"), None)
            text = read(tex_node.get("path")).decode("utf-8") if tex_node is not None else ""
            statement = {key: "" for key in ["legend", "input", "output", "scoring", "interaction", "notes", "tutorial"]}
            statement.update(name=lang_node.get("value", name), show_in_review=True)
            text = re.sub(r"\\(?:begin|end)\{document\}", "", text)
            # The complete source is retained; section bodies are split only on Polygon's section commands.
            markers = list(re.finditer(r"\\(InputFile|OutputFile|Scoring|Interaction|Note|Notes|Examples)\b", text))
            statement["legend"] = text[:markers[0].start()] if markers else text
            keys = {"InputFile": "input", "OutputFile": "output", "Scoring": "scoring", "Interaction": "interaction", "Note": "notes", "Notes": "notes"}
            for i, marker in enumerate(markers):
                if marker.group(1) in keys:
                    statement[keys[marker.group(1)]] = text[marker.end():markers[i + 1].start() if i + 1 < len(markers) else len(text)]
            doc["statements"][language] = statement
    return doc


class ImportInfo(Strict):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")


@router.post("/imports", status_code=201)
async def import_package(request: Request, name: str, slug: str, folder_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    owned_folder(db, user, folder_id)
    values = ImportInfo(name=name, slug=slug)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > get_settings().max_archive_bytes:
            fail("too_large", "압축 파일은 128MiB 이하여야 합니다.", 413)
    archive = checked_archive(bytes(content))
    if db.scalar(select(Problem).where(Problem.owner_id == user.id, Problem.slug == values.slug)):
        fail("slug_taken", "같은 이름의 문제가 이미 있습니다.", 409)
    problem = Problem(owner_id=user.id, slug=values.slug, document=empty_document(values.name))
    db.add(problem)
    db.flush()
    try:
        manifest_path = next((n for n in archive.namelist() if PurePosixPath(n).name == "editor.json"), None)
        if manifest_path:
            if archive.getinfo(manifest_path).file_size > 4 * 1024 * 1024:
                raise ValueError("Manifest too large")
            manifest = json.loads(archive.read(manifest_path))
            if manifest.get("format") != "zoj-editor-v1":
                raise ValueError("Unknown editor package version")
            doc = Document.model_validate(manifest["document"]).model_dump()
            prefix = str(PurePosixPath(manifest_path).parent)
            prefix = "" if prefix == "." else prefix + "/"
            for sha in references(doc):
                stored = put_blob(db, problem.id, archive.read(prefix + "blobs/" + sha))
                if stored["sha256"] != sha:
                    raise ValueError("Blob checksum mismatch")
        elif "manifest.json" in archive.namelist():
            from .zoj_package import restore_editor_snapshot
            doc = restore_editor_snapshot(archive, db, problem.id)
        else:
            doc = polygon_document(archive, db, problem.id, values.name)
        problem.document = validate_document(db, problem.id, doc)
        db.add(Access(problem_id=problem.id, user_id=user.id, role="OWNER"))
        place(db, user, problem.id, folder_id)
        db.add(Revision(problem_id=problem.id, number=0, author_id=user.id, message="문제 패키지 가져오기", document=copy.deepcopy(doc)))
        db.commit()
    except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError, zipfile.BadZipFile) as error:
        db.rollback()
        fail("invalid_package", f"가져오기 실패: {error}")
    finally:
        archive.close()
    return {"id": problem.id, "name": problem.document["info"]["name"]}


@router.get("/problems/{problem_id}/packages/{job_id}")
def download_package(problem_id: str, job_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id or job.kind != "package" or job.status != "completed" or not job.result.get("package"):
        fail("not_found", "완료된 패키지를 찾을 수 없습니다.", 404)
    path = get_settings().data_dir / "packages" / (job.id + ".zip")
    return Response(path.read_bytes(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{job.result["package"]["filename"]}"'})


@router.get("/problems/{problem_id}/rendered/{job_id}/{language}.{format}")
def rendered_file(problem_id: str, job_id: str, language: str, format: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    authorize(db, problem_id, user)
    job = db.get(Job, job_id)
    if not job or job.problem_id != problem_id or job.kind != "render" or job.status != "completed" or language not in job.document["statements"] or format not in {"html", "pdf"}:
        fail("not_found", "렌더링 결과를 찾을 수 없습니다.", 404)
    path = get_settings().data_dir / "rendered" / job.id / (safe_name(language) + "." + format)
    if not path.is_file():
        fail("not_found", "렌더링에 실패한 파일입니다.", 404)
    return Response(path.read_bytes(), media_type="application/pdf" if format == "pdf" else "text/html", headers={"Content-Disposition": f'attachment; filename="{language}.{format}"', "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'none'"})
