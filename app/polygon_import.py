"""Restore Polygon's authoring data; keep unmapped originals as inert Aux files."""
import hashlib
import json
import re
import shlex
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .schema import empty_document, safe_name
from .storage import put_blob

FIELDS = ("legend", "input", "output", "scoring", "interaction", "notes", "tutorial")
NAMESPACE = "{https://editor.zoj.kr/schema/v1}"


def tex_sections(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    start = re.search(r"\\begin\{problem\}", text)
    if start:
        position = start.end()
        for _ in range(5):
            while position < len(text) and text[position].isspace():
                position += 1
            if position >= len(text) or text[position] != "{":
                raise ValueError("Incomplete Polygon problem header")
            depth = 1
            position += 1
            while position < len(text) and depth:
                if text[position] == "\\":
                    position += 2
                    continue
                depth += (text[position] == "{") - (text[position] == "}")
                position += 1
            if depth:
                raise ValueError("Unbalanced Polygon problem header")
        text = text[position:]
    else:
        text = re.split(r"\\begin\{document\}", text, maxsplit=1)[-1]
    text = re.split(r"\\end\{(?:problem|document)\}", text, maxsplit=1)[0]
    markers = list(re.finditer(r"(?m)^\s*\\(InputFile|OutputFile|Scoring|Interaction|Notes?|Examples|Tutorial)\b", text))
    result = dict.fromkeys(FIELDS, "")
    result["legend"] = (text[:markers[0].start()] if markers else text).strip()
    names = {"InputFile": "input", "OutputFile": "output", "Scoring": "scoring",
             "Interaction": "interaction", "Note": "notes", "Notes": "notes", "Tutorial": "tutorial"}
    for index, marker in enumerate(markers):
        if marker[1] in names:
            result[names[marker[1]]] = text[marker.end():markers[index + 1].start() if index + 1 < len(markers) else len(text)].strip()
    return result


class PolygonImport:
    def __init__(self, archive, db, problem_id, name, report):
        self.archive, self.db, self.problem_id = archive, db, problem_id
        self.paths = set(archive.namelist())
        descriptors = [path for path in self.paths if PurePosixPath(path).name == "problem.xml"]
        if len(descriptors) != 1:
            raise ValueError("Exactly one problem.xml is required")
        self.descriptor = descriptors[0]
        parent = PurePosixPath(self.descriptor).parent
        self.prefix = "" if str(parent) == "." else str(parent) + "/"
        xml = self.read("problem.xml")
        if len(xml) > 1048576 or re.search(br"<!\s*(DOCTYPE|ENTITY)", xml, re.I):
            raise ValueError("Unsafe XML descriptor")
        self.root = ET.fromstring(xml)
        self.windows_text_tests = (
            any(node.get("type") == "exe.win32" for node in self.root.findall(".//binary"))
            and any(node.get("name") == "tests-wellformed" and node.get("value") == "true"
                    for node in self.root.findall("properties/property"))
        )
        self.normalized_test_files = set()
        self.doc = empty_document(name)
        self.doc["files"], self.doc["solutions"] = {}, {}
        self.used = set()
        self.imported = {}
        self.sample_values = {}
        self.report = report if report is not None else {"warnings": [], "preserved_files": []}

    def read(self, path):
        if not isinstance(path, str):
            raise ValueError("Missing package file path")
        part = PurePosixPath(path)
        if part.is_absolute() or ".." in part.parts or "\\" in path:
            raise ValueError(f"Unsafe descriptor path: {path}")
        target = self.prefix + str(part)
        if target not in self.paths:
            raise ValueError(f"Missing package file: {path}")
        return self.archive.read(target)

    def exists(self, path):
        return self.prefix + path in self.paths

    def text(self, path):
        try:
            return self.read(path).decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError(f"Expected UTF-8 text: {path}") from error

    def store(self, path):
        blob = put_blob(self.db, self.problem_id, self.read(path))
        self.used.add(self.prefix + str(PurePosixPath(path)))
        return blob

    def test_data(self, path):
        content = self.read(path)
        normalized = content
        if self.windows_text_tests and b"\r\n" in content and b"\x00" not in content:
            try:
                content.decode("utf-8-sig")
                normalized = content.replace(b"\r\n", b"\n")
            except UnicodeDecodeError:
                pass
        blob = put_blob(self.db, self.problem_id, normalized)
        if normalized == content:
            self.used.add(self.prefix + str(PurePosixPath(path)))
        else:
            # The original remains unused so preserve_originals saves it as Aux.
            self.normalized_test_files.add(path)
        return blob

    def warning(self, message):
        if message not in self.report["warnings"]:
            self.report["warnings"].append(message)

    def source(self, node, kind="source", solution=False):
        from .packages import guess_language
        path = node.get("path")
        name = safe_name(PurePosixPath(path).name)
        if name == "testlib.h":
            self.warning("testlib.h는 채점 환경의 공용 파일을 사용합니다. 패키지 원본은 실행에 첨부하지 않는 보조 파일로 보관합니다.")
            return None
        mapping = self.doc["solutions" if solution else "files"]
        if path in self.imported:
            old_solution, old_name = self.imported[path]
            if old_solution != solution:
                raise ValueError(f"File has ambiguous roles: {path}")
            return mapping[old_name]
        if name in self.doc["files"] or name in self.doc["solutions"]:
            raise ValueError(f"Duplicate package filename: {name}; source paths must have distinct names")
        if len(mapping) >= 200:
            raise ValueError("At most 200 files per category")
        source_type = node.get("type", "")
        language = guess_language(name, source_type)
        standard = re.search(r"g\+\+(\d+)", source_type)
        if language.startswith("cpp") and standard and int(standard[1]) > 20:
            language = "cpp20"
            self.warning(f"{name}: {source_type}는 지원 목록에 없어 C++20으로 가져왔습니다. 이 환경에서 다시 컴파일·검증하세요.")
        value = {"name": name, "kind": kind, "language": language, **self.store(path)}
        metadata = node.findtext(NAMESPACE + "metadata")
        if metadata:
            extra = json.loads(metadata)
            if not isinstance(extra, dict) or set(extra) - {"compile_with_solution", "entry_class", "roles", "resource_policy", "attach_to_build"}:
                raise ValueError("Unsupported file metadata")
            value.update(extra)
        mapping[name] = value
        self.imported[path] = (solution, name)
        return value

    def statements(self):
        self.doc["statements"] = {}
        languages = {node.get("language", "english"): node.get("value", self.doc["info"]["name"])
                     for node in self.root.findall("names/name")}
        for node in self.root.findall("statements/statement"):
            languages.setdefault(node.get("language", "english"), self.doc["info"]["name"])
        for language, title in languages.items():
            safe_name(language)
            statement = {**dict.fromkeys(FIELDS, ""), "name": title, "show_in_review": True}
            own = f"statements/{language}/properties.json"
            polygon = f"statements/{language}/problem-properties.json"
            if self.exists(own):
                statement.update(json.loads(self.text(own)))
            elif self.exists(polygon):
                properties = json.loads(self.text(polygon))
                statement.update({key: properties.get(key) or "" for key in FIELDS})
                statement["name"] = properties.get("name") or title
                samples = properties.get("sampleTests") or []
                if not isinstance(samples, list) or any(not isinstance(sample, dict) for sample in samples):
                    raise ValueError("Polygon sampleTests must be a list of examples")
                self.sample_values[language] = samples
            else:
                nodes = [node for node in self.root.findall("statements/statement") if node.get("language") == language]
                tex = next((node for node in nodes if node.get("type") == "application/x-tex"), None)
                if tex is not None:
                    statement.update(tex_sections(self.text(tex.get("path"))))
                for key in (*FIELDS, "name"):
                    path = f"statement-sections/{language}/{key}.tex"
                    if self.exists(path):
                        statement[key] = self.text(path).strip()
            self.doc["statements"][language] = statement

    def testsets(self, judging):
        from .packages import pattern_path
        self.doc["testsets"] = {}
        nodes = judging.findall("testset")
        primary = next((node for node in nodes if node.get("name") == "tests"), nodes[0] if nodes else None)
        if primary is None:
            raise ValueError("No testset descriptor")
        limits = (int(primary.findtext("time-limit", "1000")), int(primary.findtext("memory-limit", "268435456")) // 1048576)
        self.doc["info"].update(time_limit_ms=limits[0], memory_limit_mb=limits[1])
        for node in nodes:
            name = safe_name(node.get("name", "tests"))
            if name in self.doc["testsets"]:
                raise ValueError(f"Duplicate testset: {name}")
            current_limits = (int(node.findtext("time-limit", "1000")), int(node.findtext("memory-limit", "268435456")) // 1048576)
            if current_limits != limits:
                self.warning(f"{name}: 세트별 시간·메모리 제한이 달라 기본 세트의 제한을 적용했습니다. 원본 problem.xml을 함께 보관합니다.")
            tests, groups = {}, {}
            pending_inputs = missing_answers = 0
            for index, test in enumerate(node.findall("tests/test"), 1):
                path = pattern_path(node.findtext("input-path-pattern"), index)
                pattern = node.findtext("answer-path-pattern")
                answer = pattern_path(pattern, index) if pattern else None
                group = test.get("group", "")
                if group:
                    groups[group] = {"points_policy": "COMPLETE_GROUP", "feedback_policy": "COMPLETE", "dependencies": []}
                points = float(test.get("points", "0"))
                self.doc["info"]["points_enabled"] |= points > 0
                command = test.get("cmd", "")
                if self.exists(path):
                    input_sha = self.test_data(path)["sha256"]
                elif test.get("method") == "generated" and command.strip():
                    # Standard/source packages can contain commands instead of
                    # generated data. Keep the test pending, never fabricate it.
                    input_sha = None
                    pending_inputs += 1
                else:
                    raise ValueError(f"Missing package file: {path}")
                answer_sha = self.test_data(answer)["sha256"] if answer and self.exists(answer) else None
                missing_answers += answer_sha is None
                tests[str(index)] = {"index": index, "input_sha": input_sha,
                    "answer_sha": answer_sha,
                    "command": command, "description": test.get("description", ""),
                    "group": group, "points": points, "sample": test.get("sample") == "true"}
            if pending_inputs:
                self.warning(f"{name}: 아직 생성되지 않은 입력 {pending_inputs}개를 생성기 명령과 함께 가져왔습니다. ‘입력·정답 생성’을 실행하세요.")
            if missing_answers:
                self.warning(f"{name}: 정답 파일이 없는 테스트 {missing_answers}개는 Main 풀이로 정답을 생성해야 합니다.")
            for group in node.findall("groups/group"):
                groups[group.get("name")] = {"points_policy": group.get("points-policy", "complete-group").upper().replace("-", "_"),
                    "feedback_policy": group.get("feedback-policy", "complete").upper(),
                    "dependencies": [item for item in group.get("dependencies", "").split(",") if item]}
            self.doc["testsets"][name] = {"script": "", "tests": tests, "groups_enabled": bool(groups), "groups": groups}
        language = "korean" if "korean" in self.doc["statements"] else next(iter(self.doc["statements"]), None)
        samples = [test for ts in self.doc["testsets"].values() for test in ts["tests"].values() if test["sample"]]
        for index, test in enumerate(samples, 1):
            configured = self.sample_values.get(language, [])
            sample = configured[index - 1] if index <= len(configured) else {}
            for field, suffix in (("input", ""), ("output", ".a")):
                value = sample.get(field)
                path = f"statement-sections/{language}/example.{index:02d}{suffix}"
                if value is None and self.exists(path):
                    value = self.text(path)
                if value is not None:
                    test["statement_" + field] = value.replace("\r\n", "\n") if self.windows_text_tests else value
        if len(self.sample_values) > 1:
            self.warning(f"표시용 예제는 {language} 지문 기준으로 가져왔습니다. 다른 언어의 원본 지문 설정도 보조 파일로 보관합니다.")

    def assets(self):
        from .packages import TAG_NAMES, pattern_path
        for category, kind in (("sources", "source"), ("resources", "resource"), ("aux", "aux"), ("statement-resources", "statement")):
            for node in self.root.findall(f"files/{category}/file"):
                self.source(node, kind)
        for node in self.root.findall("files/executables/executable/source"):
            self.source(node)
        reverse = {value: key for key, value in TAG_NAMES.items()}
        for node in [*self.root.findall("solutions/solution"), *self.root.findall("assets/solutions/solution")]:
            src = node.find("source")
            if src is None:
                raise ValueError("A solution has no source")
            item = self.source(src, solution=True)
            if item is None:
                raise ValueError("A solution cannot use a reserved platform file")
            item.update(tag=reverse.get(node.get("tag"), node.get("tag", "OK")), extra_tags={})
            for scope in node.findall(NAMESPACE + "expectations/" + NAMESPACE + "scope"):
                group = scope.get("group")
                if group:
                    item.setdefault("group_tags", {}).setdefault(scope.get("testset"), {})[group] = scope.get("tag")
                else:
                    item["extra_tags"][scope.get("testset")] = scope.get("tag")
        for role in ("checker", "validator", "interactor"):
            nodes = [*self.root.findall("assets/" + role), *self.root.findall(f"assets/{role}s/{role}")]
            self.doc[role] = ""
            for asset in nodes:
                standard = asset.get("name", "")
                if role == "checker" and standard.startswith("std::"):
                    self.doc[role] = standard.removesuffix(".cpp")
                    self.warning("표준 Checker는 채점 환경의 공용 코드를 사용합니다. 패키지의 Checker 원본도 보조 파일로 보관합니다.")
                else:
                    node = asset.find("source")
                    if node is None:
                        raise ValueError(f"The {role} has no source")
                    item = self.source(node)
                    if item is None:
                        raise ValueError(f"The {role} uses a reserved platform file")
                    roles = item.setdefault("roles", [])
                    if role not in roles:
                        roles.append(role)
                    if role == "validator" and self.doc[role]:
                        self.doc["extra_validators"].append(item["name"])
                    elif self.doc[role] and role != "validator":
                        raise ValueError(f"Multiple {role} definitions")
                    else:
                        self.doc[role] = item["name"]
                tests = asset.find("testset")
                if tests is None or role == "interactor":
                    continue
                for index, test in enumerate(tests.findall("tests/test"), 1):
                    # The current document has one component-test list. Preserve
                    # further validators' raw test files instead of mixing suites.
                    if role == "validator" and self.doc["extra_validators"]:
                        self.warning("추가 Validator의 자체 테스트는 원본 보조 파일로 보관했습니다. 주 Validator의 자체 테스트와 합치지 않습니다.")
                        break
                    unit = {"input_sha": self.test_data(pattern_path(tests.findtext("input-path-pattern"), index))["sha256"],
                            "output_sha": None, "answer_sha": None, "expected": test.get("verdict").upper().replace("-", "_")}
                    for field, xml_key in (("output_sha", "output-path-pattern"), ("answer_sha", "answer-path-pattern")):
                        pattern = tests.findtext(xml_key)
                        if pattern:
                            unit[field] = self.test_data(pattern_path(pattern, index))["sha256"]
                    self.doc[role + "_tests"][str(index)] = unit

    def generator_roles(self):
        commands = [test["command"] for ts in self.doc["testsets"].values() for test in ts["tests"].values() if test["command"]]
        for node in self.root.findall("stresses/list/stress"):
            try:
                value = json.loads(self.text(node.get("path")))
                command = value.get("scriptPattern", "")
                if not isinstance(command, str):
                    raise ValueError("Invalid stress command")
                commands.append(command)
            except (ValueError, AttributeError):
                self.warning(f"스트레스 설정을 해석하지 못했습니다. 원본 보조 파일을 확인하세요: {node.get('path')}")
        if self.root.findall("stresses/list/stress"):
            self.warning("Polygon 스트레스 설정은 실행 기록으로 변환하지 않고 원본 JSON 보조 파일로 보관했습니다.")
        for command in commands:
            try:
                tokens = shlex.split(command)
            except ValueError:
                self.warning(f"생성기 명령의 따옴표를 확인하세요: {command[:150]}")
                continue
            if not tokens:
                continue
            matches = [item for item in self.doc["files"].values() if item["name"] == tokens[0] or PurePosixPath(item["name"]).stem == tokens[0]]
            if len(matches) == 1 and matches[0]["kind"] == "source" and matches[0]["language"] != "text":
                roles = matches[0].setdefault("roles", [])
                if "generator" not in roles:
                    roles.append("generator")
            else:
                self.warning(f"생성기 명령의 실행 파일을 확인하세요: {tokens[0]}")

    def preserve_originals(self):
        originals = sorted(path for path in self.paths - self.used if not path.endswith("/"))
        if len(originals) + len(self.doc["files"]) > 200:
            raise ValueError("The imported files and preserved originals exceed the 200-file limit")
        for path in originals:
            relative = path.removeprefix(self.prefix)
            alias = "polygon-" + re.sub(r"[^A-Za-z0-9_.-]", "-", relative.replace("/", "--"))
            alias = alias.replace("..", "--")
            if len(alias) > 100 or alias in self.doc["files"] or alias in self.doc["solutions"]:
                alias = alias[:80] + "-" + hashlib.sha256(path.encode()).hexdigest()[:12]
            safe_name(alias)
            blob = put_blob(self.db, self.problem_id, self.archive.read(path))
            self.doc["files"][alias] = {"name": alias, "kind": "aux", "language": "text", "attach_to_build": False, **blob}
            self.report["preserved_files"].append({"path": relative, "name": alias})

    def restore(self):
        judging = self.root.find("judging")
        if judging is None:
            raise ValueError("No judging descriptor")
        self.doc["info"].update(input_file=judging.get("input-file") or "stdin",
            output_file=judging.get("output-file") or "stdout", interactive=self.root.find("assets/interactor") is not None,
            well_formed=False, tags=[node.get("value") for node in self.root.findall("tags/tag")])
        self.statements()
        self.testsets(judging)
        self.assets()
        self.generator_roles()
        if self.normalized_test_files:
            self.warning(f"Windows 텍스트 테스트 파일 {len(self.normalized_test_files)}개의 줄바꿈을 Linux LF로 변환했습니다. 변환 전 원본은 보조 파일로 보관했습니다.")
        self.preserve_originals()
        return self.doc


def restore(archive, db, problem_id, name, report=None):
    return PolygonImport(archive, db, problem_id, name, report).restore()
