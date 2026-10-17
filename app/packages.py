import copy
import hashlib
import html
import io
import json
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
    from .statements import effective
    statement = effective(doc,language)
    title = html.escape(statement["name"] or doc["info"]["name"])
    sections = [("문제", "legend"), ("입력", "input"), ("출력", "output"), ("부분 점수", "scoring"), ("인터랙션", "interaction"), ("노트", "notes"), ("해설", "tutorial")]
    body = "".join(f"<section><h2>{label}</h2><div class='copy'>{html.escape(statement[key])}</div></section>" for label, key in sections if statement[key])
    from .statement_samples import display
    for testset in doc["testsets"].values():
        for test in sorted(testset["tests"].values(), key=lambda row: row["index"]):
            if test["sample"]:
                body += "<section><h2>예제 입력</h2><pre>" + html.escape(display(test, "input")) + "</pre><h2>예제 출력</h2><pre>" + html.escape(display(test, "output")) + "</pre></section>"
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
                node = ET.SubElement(category, "file", {"path": f"files/{name}", "type": SOURCE_TYPES[source["language"]]})
                metadata = {k:source[k] for k in ("compile_with_solution", "entry_class", "roles", "resource_policy", "attach_to_build") if k in source and source[k] is not None}
                if metadata:
                    ET.SubElement(node, "{https://editor.zoj.kr/schema/v1}metadata").text = json.dumps(metadata)
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


def build_package(job, testsets, report=None):
    if job.options.get("export_format") == "zoj":
        from .zoj_package import build_zoj_package
        return build_zoj_package(job, testsets)
    doc = copy.deepcopy(job.document)
    for name, tests in (testsets.items() if job.options.get("verify", True) or job.options.get("export_format") == "linux" else []):
        doc["testsets"][name]["tests"] = {str(t["index"]): {key: value for key, value in t.items() if key in {"index", "input_sha", "answer_sha", "command", "group", "points", "description", "sample", "statement_input", "statement_output", "verify_statement_io"}} for t in tests}
        doc["testsets"][name]["script"] = ""
    destination = get_settings().data_dir / "packages" / (job.id + ".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("editor.json", json.dumps({"format": "zoj-editor-v1", "revision": job.revision, "source_document_hash": job.document_hash, "verified": bool(job.options["verify"]), "document": doc}, ensure_ascii=False))
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
        if job.options.get("export_format") == "linux":
            from .linux_package import add
            add(archive, job, report)
    kind = "linux" if job.options.get("export_format") == "linux" else "editor"
    return {"filename": job.problem_id[:8] + f"-r{job.revision}" + ("-linux" if kind == "linux" else "") + ".zip", "format": kind, "size": destination.stat().st_size, "verified": bool(job.options["verify"]), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}


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


def polygon_document(archive, db, problem_id, name, report=None):
    from .polygon_import import restore
    return restore(archive, db, problem_id, name, report)


class ImportInfo(Strict):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")


@router.post("/imports", status_code=201)
async def import_package(request: Request, name: str, slug: str, folder_id: str | None = None, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    owned_folder(db, user, folder_id, permission="create_problems")
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
    report = {"format": "editor", "warnings": [], "preserved_files": []}
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
            report["format"] = "zoj"
            from .zoj_package import restore_editor_snapshot
            doc = restore_editor_snapshot(archive, db, problem.id)
        else:
            report["format"] = "polygon"
            doc = polygon_document(archive, db, problem.id, values.name, report)
        problem.document = validate_document(db, problem.id, doc)
        db.add(Access(problem_id=problem.id, user_id=user.id, role="OWNER"))
        place(db, user, problem.id, folder_id, permission="create_problems")
        db.add(Revision(problem_id=problem.id, number=0, author_id=user.id, message="문제 패키지 가져오기", document=copy.deepcopy(problem.document)))
        db.commit()
    except (ValueError, KeyError, TypeError, AttributeError, ET.ParseError, zipfile.BadZipFile) as error:
        db.rollback()
        fail("invalid_package", f"가져오기 실패: {error}")
    finally:
        archive.close()
    report.update(files=len(problem.document["files"]), solutions=len(problem.document["solutions"]),
                  tests=sum(len(ts["tests"]) for ts in problem.document["testsets"].values()),
                  statement_languages=list(problem.document["statements"]))
    return {"id": problem.id, "name": problem.document["info"]["name"], "import_report": report}


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
