"""ZOJ problem ZIP v1 adapter. Do not silently change judging semantics."""
import copy
import hashlib
import json
import mimetypes
import re
import zipfile
from datetime import datetime, timezone
from .config import get_settings
from .domain import fail, references
from .schema import Document
from .storage import put_blob, read_blob
from .sandbox import VENDOR


def statement_language_code(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("ZOJ 지문 언어는 영문 언어 코드로 지정하세요.")
    aliases = {"korean": "ko", "english": "en", "japanese": "ja", "chinese": "zh", "russian": "ru",
               "german": "de", "french": "fr", "spanish": "es", "portuguese": "pt"}
    parts = aliases.get(value.lower(), value).replace("_", "-").split("-")
    return "-".join([parts[0].lower(), *(
        part.title() if len(part) == 4 and part.isalpha()
        else part.upper() if len(part) == 2 and part.isalpha()
        else part.lower() for part in parts[1:])])


def check_compatibility(doc, options):
    issues = []
    info = doc["info"]
    if "editor-project.json" in doc["files"] or "editor-project.json" in doc["solutions"]:
        issues.append("editor-project.json은 ZOJ 작업 자료용 파일명입니다. 파일명을 바꾸세요.")
    if info["interactive"] or info["input_file"] != "stdin" or info["output_file"] != "stdout":
        issues.append("ZOJ는 현재 표준 입출력의 비대화형 문제를 지원합니다.")
    if info["points_enabled"] or any(s["groups_enabled"] for s in doc["testsets"].values()):
        issues.append("그룹·부분 점수 채점은 현재 ZOJ 문제 ZIP에 표현할 수 없습니다.")
    if not 100 <= info["time_limit_ms"] <= 10000 or not 16 <= info["memory_limit_mb"] <= 1024:
        issues.append("ZOJ 제한 범위는 시간 100~10,000ms, 메모리 16~1,024MiB입니다.")
    if doc["extra_validators"] or any(f.get("compile_with_solution") for f in doc["files"].values()):
        issues.append("추가 validator와 풀이 결합 파일은 ZOJ 채점에서 동일하게 실행되지 않습니다.")
    if any(f.get("resource_policy") for f in doc["files"].values()):
        issues.append("언어·대상별 리소스 정책은 Editor 또는 재현용 패키지로 내보내세요. ZOJ 문제 ZIP은 이 컴파일 정책을 지원하지 않습니다.")
    checker = doc["files"].get(doc["checker"])
    if checker and checker["language"] not in {"cpp17", "c17", "c99", "python3"}:
        issues.append("ZOJ checker는 C++17·C·Python 소스만 지원합니다. 해당 언어로 검증하세요.")
    if options["statement_language"] not in doc["statements"]:
        issues.append("내보낼 지문 언어를 선택하세요.")
    try:
        codes = [statement_language_code(name) for name in doc["statements"]]
        if len(set(codes)) != len(codes):
            issues.append("같은 언어 코드에 해당하는 지문이 여러 개입니다. 언어를 정리한 뒤 내보내세요.")
    except ValueError as error:
        issues.append(str(error))
    if issues:
        fail("zoj_incompatible", " ".join(issues), 400, issues=issues)


def build_zoj_package(job, testsets):
    check_compatibility(job.document, job.options)
    if not testsets.get(job.options["testset"]):
        raise ValueError("ZOJ에 활성화할 테스트 세트에는 입력·정답이 있어야 합니다.")
    doc = copy.deepcopy(job.document)
    for name, tests in testsets.items():
        doc["testsets"][name]["script"] = ""
        doc["testsets"][name]["tests"] = {str(t["index"]): {k: v for k, v in t.items() if k in {"index", "input_sha", "answer_sha", "command", "group", "points", "description", "sample", "statement_input", "statement_output", "verify_statement_io"}} for t in tests}
    destination = get_settings().data_dir / "packages" / (job.id + ".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"format": "zoj-problem", "version": 1, "problem": {}, "assets": [], "testcase_sets": [], "files": []}
    mappings = {}
    created = datetime.now(timezone.utc).isoformat()
    total = 0
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        def file(path, content):
            nonlocal total
            sha = hashlib.sha256(content).hexdigest()
            if sha in mappings:
                return mappings[sha]
            total += len(content)
            if len(content) > 256 * 1048576 or total > 2 * 1024**3 or len(manifest["files"]) >= 19998:
                raise ValueError("ZOJ 패키지의 파일 크기·개수 한도를 초과했습니다.")
            archive.writestr(path, content)
            manifest["files"].append({"path": path, "size": len(content), "sha256": sha})
            mappings[sha] = path
            return path
        def asset(name, content, category):
            index = len(manifest["assets"]) + 1
            if index > 10000:
                raise ValueError("ZOJ 패키지의 첨부 파일 한도를 초과했습니다.")
            path = file(f"assets/{index:05d}/{name}", content)
            identifier = f"asset-{index}"
            manifest["assets"].append({"id": identifier, "file": path, "original_filename": name,
                "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream", "category": category,
                "asset_status": "active", "created_at": created})
            return identifier
        images = {}
        generators = {t.get("command", "").split()[0] for tests in testsets.values() for t in tests if t.get("command")}
        for name, source in doc["files"].items():
            role = next((role for role in ("checker", "validator", "interactor") if doc[role] == name), None)
            if role:
                category = "package-files/" + role
            elif name in generators:
                category = "package-files/generator"
            else:
                category = "assets" if source["kind"] == "statement" else "package-files/package-resource"
            aid = asset(name, read_blob(source["sha256"]), category)
            if source["kind"] == "statement":
                images[name] = "asset://" + aid
        if doc["checker"].startswith("std::"):
            name = doc["checker"].removeprefix("std::") + ".cpp"
            asset("std-" + name, (VENDOR / name).read_bytes(), "package-files/checker")
        asset("testlib.h", (VENDOR / "testlib.h").read_bytes(), "package-files/package-resource")
        asset("TESTLIB-LICENSE", (VENDOR / "LICENSE").read_bytes(), "package-files/package-resource")
        status = {"MA": "accepted", "OK": "accepted", "WA": "wrong_answer", "TL": "time_limit_exceeded", "ML": "memory_limit_exceeded"}
        for name, solution in doc["solutions"].items():
            content = read_blob(solution["sha256"])
            if solution["tag"] == "MA":
                asset(name, content, "package-files/main-solution")
            category = "verification-solutions/" + status[solution["tag"]] if solution["tag"] in status else "package-files/wrong-solution"
            asset(name, content, category)
        examples = []
        count = 0
        for version, (set_name, tests) in enumerate(testsets.items(), 1):
            cases = []
            active = set_name == job.options["testset"]
            for test in tests:
                count += 1
                if count > 10000:
                    raise ValueError("ZOJ 패키지는 전체 테스트 10,000개까지 지원합니다.")
                inp, out = read_blob(test["input_sha"]), read_blob(test["answer_sha"])
                path = f"testcases/v{version}/{test['index']:05d}"
                cases.append({"display_order": test["index"], "input": file(path + ".in", inp), "output": file(path + ".out", out),
                    "time_limit_ms_override": None, "memory_limit_mb_override": None})
                if active and test["sample"]:
                    from .statement_samples import display
                    examples.append({"input": display(test, "input"), "output": display(test, "output"), "note": test["description"]})
            manifest["testcase_sets"].append({"version": version, "is_active": active, "testcases": cases})
        for sha in references(doc) | references(job.document):
            if sha not in mappings:
                asset("blob-" + sha + ".bin", read_blob(sha), "package-files/package-resource")
        # The source snapshot preserves Editor-only metadata for reimport.
        snapshot = {"format": "zoj-editor-source-v1", "document": doc, "source_document": job.document,
                    "revision": job.revision, "document_hash": job.document_hash, "blobs": dict(mappings)}
        encoded_snapshot = json.dumps(snapshot, ensure_ascii=False).encode()
        if len(encoded_snapshot) > get_settings().max_upload_bytes:
            raise ValueError("Editor 작업 자료가 개별 파일 16MiB 한도를 초과했습니다.")
        # The snapshot includes editorials and solution metadata; it is never a
        # public statement attachment, even when some statements are published.
        asset("editor-project.json", encoded_snapshot, "package-files/package-resource")
        default_language = job.options["statement_language"]
        statement = doc["statements"][default_language]
        def rewrite(text):
            # Match full Markdown resource targets, never arbitrary occurrences.
            return re.sub(r"(?<=\]\()(?:\.\/)?([^\s)]+)(?=\))", lambda m: images.get(m[1], m[0]), text)
        def statement_text(item):
            meta = {"inputDescription": rewrite(item["input"]), "outputDescription": rewrite(item["output"]),
                    "note": rewrite(item["notes"]), "examples": examples}
            return "<!--ZOJ_META:" + json.dumps(meta, ensure_ascii=False).replace("-->", "\\u002d\\u002d>") + "-->\n" + rewrite(item["legend"])
        manifest["problem"] = {"problem_code": "A", "title": statement["name"] or doc["info"]["name"],
            "statement": statement_text(statement), "editorial": rewrite(statement["tutorial"]), "time_limit_ms": doc["info"]["time_limit_ms"],
            "memory_limit_mb": doc["info"]["memory_limit_mb"], "language_resource_limits": {}, "display_order": 1}
        # Expose translations through ZOJ's native fields, separate from the
        # private Editor snapshot. Single-language Korean archives stay unchanged.
        language_codes = {name: statement_language_code(name) for name in doc["statements"]}
        if len(doc["statements"]) > 1 or language_codes[default_language] != "ko":
            manifest["problem"].update(default_statement_language=language_codes[default_language],
                statement_translations={language_codes[name]: {"title": item["name"] or doc["info"]["name"], "statement": statement_text(item)}
                    for name, item in doc["statements"].items() if name != default_language and item["legend"].strip()},
                editorial_translations={language_codes[name]: rewrite(item["tutorial"])
                    for name, item in doc["statements"].items() if name != default_language and item["tutorial"].strip()})
        encoded = json.dumps(manifest, ensure_ascii=False).encode()
        if len(encoded) > 8 * 1048576 or total + len(encoded) > 2 * 1024**3:
            raise ValueError("ZOJ 문제 메타데이터의 크기 한도를 초과했습니다.")
        archive.writestr("manifest.json", encoded)
    if destination.stat().st_size > get_settings().max_archive_bytes:
        destination.unlink()
        raise ValueError("패키지 크기가 Editor의 128MiB 한도를 초과했습니다.")
    return {"filename": job.problem_id[:8] + f"-r{job.revision}.zoj.zip", "format": "zoj", "size": destination.stat().st_size,
        "verified": bool(job.options["verify"]), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "testset": job.options["testset"], "statement_language": job.options["statement_language"]}


def restore_editor_snapshot(archive, db, problem_id):
    manifest = json.loads(archive.read("manifest.json"))
    if manifest.get("format") != "zoj-problem" or manifest.get("version") != 1:
        raise ValueError("지원하지 않는 ZOJ ZIP 버전입니다.")
    for item in manifest["files"]:
        content = archive.read(item["path"])
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("ZOJ ZIP 파일 무결성 검사가 실패했습니다.")
    item = next((a for a in manifest["assets"] if a["original_filename"] == "editor-project.json" and a["category"] in {"assets", "package-files/package-resource"}), None)
    if not item:
        raise ValueError("이 ZOJ ZIP에는 Editor 작업 자료가 없습니다. Editor에서 내보낸 ZOJ ZIP을 선택하세요.")
    snapshot = json.loads(archive.read(item["file"]))
    if snapshot.get("format") != "zoj-editor-source-v1":
        raise ValueError("지원하지 않는 Editor 작업 자료입니다.")
    doc = Document.model_validate(snapshot["document"]).model_dump()
    # ZOJ reexports use new archive paths. Restore by validated content hashes,
    # not the paths from the original Editor export retained in the snapshot.
    file_by_sha = {item["sha256"]: item["path"] for item in manifest["files"]}
    for sha in references(doc):
        path = file_by_sha.get(sha)
        if not path or put_blob(db, problem_id, archive.read(path))["sha256"] != sha:
            raise ValueError("Editor 작업 자료의 체크섬이 다릅니다.")
    return doc
