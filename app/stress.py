"""Deterministic generator patterns and observed counterexample coverage."""
import hashlib
import itertools
from pathlib import Path
import re
import shlex

from .execution_limits import MAX_GENERATOR_ARGUMENTS, MAX_STRESS_PATTERNS

DEFAULT_VERDICTS = ["WA", "TLE", "MLE", "RE", "PE", "OLE", "IDLENESS_LIMIT", "PARTIAL"]
RANGE = re.compile(r"\[(-?\d+)\.\.(-?\d+)\]")
SAMPLING_VERSION = "sha256-range-v1"


def patterns_for(options):
    return options.get("patterns") or [{"name": "패턴 1", "command": options.get("generator", "")}]


def stop_mode(options):
    return options.get("stop_mode") or ("first" if options.get("stop_on_first", True) else "target")


def validate_patterns(document, options):
    patterns = patterns_for(options)
    if not 1 <= len(patterns) <= MAX_STRESS_PATTERNS:
        raise ValueError(f"생성기 패턴은 1~{MAX_STRESS_PATTERNS}개를 지정하세요.")
    for pattern in patterns:
        command = pattern["command"]
        try:
            tokens = shlex.split(command)
        except ValueError as error:
            raise ValueError("생성기 명령의 따옴표를 확인하세요.") from error
        if not tokens or len(tokens) > MAX_GENERATOR_ARGUMENTS + 1 or "{seed}" in tokens[0] or RANGE.search(tokens[0]):
            raise ValueError("생성기 파일과 올바른 인자를 지정하세요.")
        if any(token in {"|", "||", "&&", ";", ">", "<", ">>"} for token in tokens):
            raise ValueError("셸 연산자 대신 패턴 추가를 사용하세요.")
        name = tokens[0]
        if name not in document["files"]:
            matches = [filename for filename in document["files"] if Path(filename).stem == name]
            if len(matches) != 1:
                raise ValueError(f"생성기 파일을 찾을 수 없습니다: {name}")
            name = matches[0]
        source = document["files"][name]
        if source["kind"] != "source" or source["language"] == "text":
            raise ValueError("생성기는 실행 가능한 소스 파일이어야 합니다.")
        for match in RANGE.finditer(command):
            low, high = map(int, match.groups())
            if not -(2**63) <= low <= high < 2**63:
                raise ValueError("난수 범위는 작은 값부터 큰 값까지 64비트 정수로 지정하세요.")
        if re.search(r"\[[^\]\n]*\.\.", RANGE.sub("0", command)):
            raise ValueError("난수 범위는 [최솟값..최댓값] 형식의 정수로 지정하세요.")
        # Appending the implicit seed must also respect the argument limit.
        if "{seed}" not in command and len(tokens) > MAX_GENERATOR_ARGUMENTS:
            raise ValueError("자동으로 추가되는 시드 인자를 포함해 생성기 인자 한도를 넘습니다.")
    return patterns


def generator_command(template, seed, pattern_index=0, draws=None):
    index = 0
    def sample(match):
        nonlocal index
        low, high = map(int, match.groups())
        if not -(2**63) <= low <= high < 2**63:
            raise ValueError("Invalid generator integer range")
        span, nonce = high - low + 1, 0
        ceiling = (1 << 256) - (1 << 256) % span
        while True:
            payload = f"{SAMPLING_VERSION}:{seed}:{pattern_index}:{index}:{nonce}".encode()
            number = int.from_bytes(hashlib.sha256(payload).digest(), "big")
            if number < ceiling:
                break
            nonce += 1
        value = low + number % span
        if draws is not None:
            draws.append({"expression": match.group(), "value": value})
        index += 1
        return str(value)
    command = RANGE.sub(sample, template)
    return command.replace("{seed}", str(seed)) if "{seed}" in template else command + " " + str(seed)


def minimum_cover(examples):
    """Cover observed (solution, verdict) pairs, without inferring root causes."""
    rows = [(example, {(finding["solution"], finding["verdict"]) for finding in example["findings"]}) for example in examples]
    rows = [(example, covered) for example, covered in rows if covered]
    universe = set().union(*(covered for _, covered in rows))
    ordered = sorted(rows, key=lambda row: (row[0].get("input_bytes", 0), row[0]["id"]))
    exact = len(ordered) <= 16
    selected = []
    if exact:
        for count in range(1, len(ordered) + 1):
            found = next((subset for subset in itertools.combinations(ordered, count) if set().union(*(covered for _, covered in subset)) == universe), None)
            if found is not None:
                selected = list(found)
                break
    else:
        uncovered = set(universe)
        while uncovered:
            chosen = max(ordered, key=lambda row: len(row[1] & uncovered))
            selected.append(chosen)
            uncovered -= chosen[1]
        for row in list(reversed(selected)):
            remaining = [item for item in selected if item[0]["id"] != row[0]["id"]]
            if set().union(*(covered for _, covered in remaining)) == universe:
                selected = remaining
    return {"example_ids": [example["id"] for example, _ in selected], "method": "exact" if exact else "greedy",
            "covered_pairs": len(universe), "pairs": [{"solution": name, "verdict": verdict} for name, verdict in sorted(universe)]}


def stress_configuration(document, options):
    reference = options.get("reference") or next(
        (name for name, source in document["solutions"].items() if source["tag"] == "MA"), ""
    )
    candidates = list(dict.fromkeys(options.get("candidates") or ([options["candidate"]] if options.get("candidate") else [])))
    return {
        "generator": options["generator"],
        "patterns": patterns_for(options),
        "sampling_version": SAMPLING_VERSION,
        "seed_start": options.get("seed_start", 1),
        "seed_step": options.get("seed_step", 1),
        "seed_mode": "placeholder" if "{seed}" in options["generator"] else "append",
        "reference": reference,
        "candidates": [{"name": name, "tag": document["solutions"][name]["tag"]} for name in candidates],
        "testset": options["testset"],
        "iterations": options["iterations"],
        "duration_seconds": options.get("duration_seconds"),
        "stop_on_first": options.get("stop_on_first", True),
        "counterexample_limit": options.get("counterexample_limit", 1),
        "counterexample_verdicts": list(options.get("counterexample_verdicts") or DEFAULT_VERDICTS),
        "stop_mode": stop_mode(options),
        "focus_uncovered": options.get("focus_uncovered", False) or stop_mode(options) == "all_candidates",
        "group": options.get("group", ""),
        "minimize": options.get("minimize", False),
        "minimize_attempts": options.get("minimize_attempts", 40),
        "minimize_seconds": options.get("minimize_seconds", 10),
        "time_limit_ms": document["info"]["time_limit_ms"],
        "memory_limit_mb": document["info"]["memory_limit_mb"],
        "validator": document["validator"],
        "extra_validators": list(document["extra_validators"]),
        "checker": document["checker"],
        "interactor": document["interactor"] if document["info"]["interactive"] else "",
    }
