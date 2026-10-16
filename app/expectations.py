"""Expected verdicts with explicit testset and group scopes."""
from .domain import expanded_tests

VERSION = 2


def expected_tag(source, set_name, group="", testsets=()):
    if source["tag"] == "NR":
        return "NR", "default"
    groups = source.get("group_tags", {}).get(set_name, {})
    if group in groups:
        return groups[group], "group"
    # Read legacy unqualified group tags, but never confuse a set with a group.
    extra = source.get("extra_tags", {})
    if group and group in extra and group != set_name and group not in testsets:
        return extra[group], "legacy_group"
    return extra.get(set_name, source["tag"]), "testset" if set_name in extra else "default"


def matches(tag, verdicts):
    failures = set(verdicts) - {"AC"}
    if tag in {"MA", "OK"}:
        return bool(verdicts) and not failures
    if tag == "NR":
        return True
    if "JUDGE_ERROR" in failures or "CE" in failures:
        return False
    if tag == "RJ":
        return bool(failures)
    if tag == "TO":
        return bool(verdicts) and failures <= {"TLE"}
    if tag == "TM":
        return bool(failures) and failures <= {"TLE", "MLE"}
    return {"WA": "WA", "TL": "TLE", "ML": "MLE", "RE": "RE", "PE": "PE"}.get(tag) in failures


def evaluate(document, source, prepared, rows):
    """Overrides replace the default for their scope; unknown cases stay unknown."""
    buckets = {}
    for set_name in prepared:
        for test in expanded_tests(document["testsets"][set_name]):
            tag, origin = expected_tag(source, set_name, test.get("group", ""), document["testsets"])
            group = test.get("group", "") if origin in {"group", "legacy_group"} else ""
            # The source tag describes one combined default scope. Only
            # explicit testset/group overrides impose independent conditions.
            key = ("default",) if origin == "default" else (set_name, group)
            bucket = buckets.setdefault(key, {"testset": set_name, "group": group, "tag": tag,
                                               "testsets": [], "origin": origin, "indices": set()})
            if set_name not in bucket["testsets"]:
                bucket["testsets"].append(set_name)
            bucket["indices"].add((set_name, test["index"]))
    errors = [row for row in rows if row["index"] == 0]
    scopes = []
    for bucket in buckets.values():
        observed = [row for row in rows if (row["testset"], row["index"]) in bucket["indices"]]
        # Do not evaluate unrequested groups as if they had run.
        requested = {(set_name, test["index"]) for set_name in bucket["testsets"] for test in prepared[set_name]}
        if not bucket["indices"] & requested:
            continue
        verdicts = [row["verdict"] for row in observed]
        tag = bucket["tag"]
        complete = tag == "NR" or {(row["testset"], row["index"]) for row in observed} == bucket["indices"]
        matched = matches(tag, verdicts) if complete or verdicts else None
        if not complete and matched is False and tag not in {"MA", "OK", "TO"} and not set(verdicts) & {"CE", "JUDGE_ERROR"}:
            matched = None
        scopes.append({key: value for key, value in bucket.items() if key != "indices"} | {
            "testset": bucket["testset"] if len(bucket["testsets"]) == 1 else "전체",
            "tested": len(observed), "total": len(bucket["indices"]), "complete": complete,
            "matched": matched, "skipped": tag == "NR", "verdicts": sorted(set(verdicts)),
        })
    matched = bool(scopes) and not errors and all(scope["matched"] is not False for scope in scopes)
    return {"matched": matched, "expectations": scopes,
            "expectations_version": VERSION,
            "expectations_complete": bool(scopes) and not errors and all(scope["complete"] for scope in scopes)}


def refresh_result(document, result):
    """Recompute derived matches for saved verification, never rerun or edit cases."""
    if result.get("expectations_version") == VERSION or not result.get("testsets") or not result.get("solutions"):
        return result
    solutions = []
    for saved in result["solutions"]:
        source = document["solutions"].get(saved["name"])
        if source is None:
            return result  # An incomplete legacy snapshot is not new evidence.
        if saved.get("skipped"):
            solutions.append(saved)
            continue
        expected = evaluate(document, source, result["testsets"], saved.get("cases", []))
        solutions.append({**saved, **expected, "matched": saved.get("complete", True) and expected["matched"]})
    mismatches = [solution["name"] for solution in solutions if not solution["matched"]]
    verified = not mismatches and all(solution.get("expectations_complete", True) for solution in solutions)
    return {**result, "solutions": solutions, "mismatches": mismatches, "verified": verified,
            "expectations_version": VERSION,
            "summary": "저장된 채점 결과 · 기대 판정 확인 완료" if verified else "저장된 채점 결과 · 기대 판정 불일치 또는 검증 미완료"}
