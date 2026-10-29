from .file_limits import MAX_FILE_BYTES
import re
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer
from .execution_limits import MAX_TESTS_PER_SET


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LanguageResourceLimit(Strict):
    time_limit_ms: int | None = Field(default=None, ge=100, le=10000, strict=True)
    memory_limit_mb: int | None = Field(default=None, ge=16, le=1024, strict=True)
    disable_auto_adjustment: bool = Field(default=False, strict=True)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        return {key:value for key,value in handler(self).items() if value is not None and (key != "disable_auto_adjustment" or value)}


class Info(Strict):
    name: str = Field(min_length=1, max_length=200)
    input_file: str = Field(default="stdin", min_length=1, max_length=64)
    output_file: str = Field(default="stdout", min_length=1, max_length=64)
    time_limit_ms: int = Field(default=1000, ge=250, le=15000, multiple_of=50)
    memory_limit_mb: int = Field(default=256, ge=4, le=1024)
    interactive: bool = False
    well_formed: bool = True
    skip_duplicate_validation: bool = False
    points_enabled: bool = False
    checker_percent: bool = False
    tags: list[str] = Field(default_factory=list, max_length=30)
    description: str = Field(default="", max_length=16384)

    @field_validator("name")
    @classmethod
    def single_line(cls, value):
        if any(ch in value for ch in "\r\n\u2028\u2029"):
            raise ValueError("The problem name must be one line")
        return value

    @field_validator("input_file", "output_file")
    @classmethod
    def file_name(cls, value):
        return safe_name(value)

    @model_validator(mode="after")
    def check_info(self):
        if self.input_file.lower() == self.output_file.lower():
            raise ValueError("Input and output file names must differ")
        if self.checker_percent and not self.points_enabled:
            raise ValueError("Enable points before checker percentages")
        return self


def safe_name(value: str):
    if value in {"__proto__", "constructor", "prototype"} or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}", value) or ".." in value:
        raise ValueError("Use a plain ASCII filename without directories")
    return value


class Statement(Strict):
    name: str = Field(default="", max_length=200)
    legend: str = Field(default="", max_length=262144)
    input: str = Field(default="", max_length=65536)
    output: str = Field(default="", max_length=65536)
    scoring: str = Field(default="", max_length=65536)
    interaction: str = Field(default="", max_length=65536)
    notes: str = Field(default="", max_length=65536)
    tutorial: str = Field(default="", max_length=262144)
    show_in_review: bool = True

    @field_validator("name")
    @classmethod
    def single_line(cls, value):
        return Info.single_line(value)


class ResourcePolicy(Strict):
    languages: list[Literal["cpp17", "cpp20", "c17", "c99", "python3", "pypy3", "kotlin", "java17", "java8"]] = Field(min_length=1, max_length=9)
    targets: list[Literal["SOLUTION"]] = Field(default_factory=lambda:["SOLUTION"], min_length=1, max_length=1)
    stages: list[Literal["COMPILE"]] = Field(default_factory=lambda:["COMPILE"], min_length=1, max_length=1)


class SourceFile(Strict):
    name: str
    kind: Literal["source", "resource", "aux", "statement"] = "source"
    language: Literal["cpp17", "cpp20", "c17", "c99", "python3", "pypy3", "kotlin", "java17", "java8", "text"] = "cpp17"
    compile_with_solution: bool = False
    resource_policy: ResourcePolicy | None = None
    attach_to_build: bool | None = None
    entry_class: str = Field(default="", pattern=r"^(?:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)?$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=MAX_FILE_BYTES)
    roles: list[Literal["validator", "checker", "generator", "interactor"]] = Field(default_factory=list, max_length=4)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        # Preserve hashes of legacy documents that have no role metadata.
        if not self.roles:
            value.pop("roles", None)
        for key in ("resource_policy", "attach_to_build"):
            if value[key] is None: value.pop(key)
        return value

    @model_validator(mode="after")
    def executable_roles(self):
        if self.resource_policy and self.kind != "resource":
            raise ValueError("Resource policies require Resource files")
        if self.roles and (self.kind != "source" or self.language == "text"):
            raise ValueError("Program roles require executable source files")
        if len(self.roles) != len(set(self.roles)):
            raise ValueError("Program roles must be unique")
        return self
    @field_validator("name")
    @classmethod
    def check_name(cls, value):
        if value in {"testlib.h", "editor.json", "problem.xml", "TESTLIB-LICENSE", "program"} or value in {"std-" + n + ".cpp" for n in ["wcmp", "fcmp", "ncmp", "rcmp4", "rcmp6", "rcmp9"]}:
            raise ValueError("Reserved platform file")
        return safe_name(value)


ExpectationTag = Literal["OK", "RJ", "TL", "TO", "TM", "WA", "PE", "ML", "NR", "RE"]


class Solution(SourceFile):
    tag: Literal["MA", "OK", "RJ", "TL", "TO", "TM", "WA", "PE", "ML", "NR", "RE"] = "OK"
    extra_tags: dict[str, ExpectationTag] = Field(default_factory=dict)
    group_tags: dict[str, dict[str, ExpectationTag]] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = super().serialize(handler)
        if not self.group_tags:
            value.pop("group_tags", None)
        return value


class Test(Strict):
    index: int = Field(ge=1, le=MAX_TESTS_PER_SET)
    input_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    answer_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    command: str = Field(default="", max_length=4096)
    group: str = Field(default="", max_length=64)
    points: float = Field(default=0, ge=0, le=1e9)
    description: str = Field(default="", max_length=200)
    sample: bool = False
    statement_input: str | None = Field(default=None, max_length=65536)
    statement_output: str | None = Field(default=None, max_length=65536)
    verify_statement_io: bool = True

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        for key in ("statement_input", "statement_output"):
            if value[key] is None: value.pop(key)
        if self.verify_statement_io and self.statement_input is None and self.statement_output is None:
            value.pop("verify_statement_io", None)
        return value
    @model_validator(mode="after")
    def valid_test(self):
        if not self.input_sha and not self.command:
            raise ValueError("Provide input or a generator command")
        return self


class Group(Strict):
    points_policy: Literal["COMPLETE_GROUP", "EACH_TEST"] = "COMPLETE_GROUP"
    feedback_policy: Literal["NONE", "POINTS", "ICPC", "COMPLETE"] = "COMPLETE"
    dependencies: list[str] = Field(default_factory=list, max_length=100)


class TestSet(Strict):
    order: list[Annotated[int, Field(strict=True, ge=1, le=MAX_TESTS_PER_SET)]] = Field(default_factory=list, max_length=MAX_TESTS_PER_SET)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if not self.order:
            value.pop("order", None)
        return value

    script: str = Field(default="", max_length=262144)
    tests: dict[str, Test] = Field(default_factory=dict)
    groups_enabled: bool = False
    groups: dict[str, Group] = Field(default_factory=dict)
    @model_validator(mode="after")
    def valid_set(self):
        if len(set(self.order)) != len(self.order):
            raise ValueError("Test order must contain unique indexes")
        if len(self.groups) > 100:
            raise ValueError("At most 100 test groups")
        for name in self.groups:
            safe_name(name)
        done = set()
        if len(self.tests) > MAX_TESTS_PER_SET:
            raise ValueError(f"At most {MAX_TESTS_PER_SET} tests per testset")
        if len({t.index for t in self.tests.values()}) != len(self.tests):
            raise ValueError("Test indexes must be unique")
        for t in self.tests.values():
            if t.group and (not self.groups_enabled or t.group not in self.groups):
                raise ValueError("Test group must exist and groups must be enabled")
        def visit(name, trail):
            if name in done:
                return
            if name in trail:
                raise ValueError("Cyclic test group dependency")
            if name not in self.groups:
                raise ValueError("Unknown test group dependency")
            for dependency in self.groups[name].dependencies:
                visit(dependency, trail | {name})
            done.add(name)
        for name in self.groups:
            visit(name, set())
        return self


class UnitTest(Strict):
    input_sha: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    answer_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    expected: Literal["VALID", "INVALID", "OK", "WRONG_ANSWER", "PRESENTATION_ERROR", "CRASHED"]


class Material(Strict):
    strategy: Literal["NONE", "WITH_STATEMENT", "WITH_TUTORIAL"] = "NONE"
    files: list[str] = Field(default_factory=list)
    solutions: list[str] = Field(default_factory=list)
    testsets: list[str] = Field(default_factory=list)


class TranslationReview(Strict):
    source_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class Document(Strict):
    language_resource_limits: dict[Literal["cpp17", "python3", "c99", "java8", "pypy3", "kotlin"], LanguageResourceLimit] = Field(default_factory=dict, max_length=6)
    info: Info
    common_statement: str = Field(default="", max_length=262144)
    common_tutorial: str = Field(default="", max_length=262144)
    translation_source: str | None = None
    translation_reviews: dict[str,TranslationReview] = Field(default_factory=dict,max_length=20)
    statements: dict[str, Statement] = Field(default_factory=dict)
    files: dict[str, SourceFile] = Field(default_factory=dict)
    solutions: dict[str, Solution] = Field(default_factory=dict)
    checker: str = "std::wcmp"
    validator: str = ""
    extra_validators: list[str] = Field(default_factory=list)
    interactor: str = ""
    testsets: dict[str, TestSet] = Field(default_factory=lambda: {"tests": TestSet()})
    validator_tests: dict[str, UnitTest] = Field(default_factory=dict)
    checker_tests: dict[str, UnitTest] = Field(default_factory=dict)
    materials: dict[str, Material] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value=handler(self)
        value["language_resource_limits"] = {key:limits for key,limits in value["language_resource_limits"].items() if limits}
        for key in ("common_statement","common_tutorial","translation_source","translation_reviews","language_resource_limits"):
            if not value[key]: value.pop(key)
        return value

    @model_validator(mode="after")
    def validate_references(self):
        if self.translation_source and self.translation_source not in self.statements:
            raise ValueError("The source statement language must exist")
        if set(self.translation_reviews)-set(self.statements):
            raise ValueError("Translation reviews must refer to existing languages and content hashes")
        if len(self.validator_tests) > 1000 or len(self.checker_tests) > 1000 or len(self.materials) > 100:
            raise ValueError("Too many component tests or materials")
        for unit in self.validator_tests.values():
            if unit.expected not in {"VALID", "INVALID"}:
                raise ValueError("Validator tests require VALID or INVALID")
        for unit in self.checker_tests.values():
            if not unit.output_sha or not unit.answer_sha or unit.expected not in {"OK", "WRONG_ANSWER", "PRESENTATION_ERROR", "CRASHED"}:
                raise ValueError("Checker tests require input, output, answer and checker verdict")
        for mapping in [self.files, self.solutions]:
            if len(mapping) > 200:
                raise ValueError("At most 200 files per category")
            for name, file in mapping.items():
                if name != file.name:
                    raise ValueError("File key must match filename")
        if len(self.testsets) > 20 or len(self.statements) > 20:
            raise ValueError("At most 20 testsets or statement languages")
        for name in [*self.testsets, *self.statements, *self.materials]:
            safe_name(name)
        for name in [self.validator, self.interactor, *self.extra_validators]:
            if name and (name not in self.files or self.files[name].kind != "source"):
                raise ValueError(f"Unknown source: {name}")
        if self.checker not in {"std::wcmp", "std::fcmp", "std::ncmp", "std::rcmp4", "std::rcmp6", "std::rcmp9", ""} and self.checker not in self.files:
            raise ValueError("Unknown checker")
        if len([s for s in self.solutions.values() if s.tag == "MA"]) > 1:
            raise ValueError("Only one Main solution is allowed")
        for source in self.solutions.values():
            for scope in source.extra_tags:
                if scope not in self.testsets and sum(scope in ts.groups for ts in self.testsets.values()) != 1:
                    raise ValueError(f"Unknown or ambiguous expectation scope: {scope}")
            for set_name, groups in source.group_tags.items():
                if set_name not in self.testsets or set(groups) - set(self.testsets[set_name].groups):
                    raise ValueError("Expected-verdict groups must exist in their testset")
        for name, material in self.materials.items():
            if any(f not in self.files for f in material.files) or any(s not in self.solutions for s in material.solutions) or any(t not in self.testsets for t in material.testsets):
                raise ValueError(f"Invalid material reference: {name}")
        return self


def empty_document(name: str) -> dict:
    return Document(info=Info(name=name), statements={"korean": Statement(name=name)}).model_dump()
