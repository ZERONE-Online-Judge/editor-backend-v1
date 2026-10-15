"""Expected verdicts with explicit testset and group scopes."""
from .domain import expanded_tests


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
            key = set_name, group
            bucket = buckets.setdefault(key, {"testset": set_name, "group": group, "tag": tag,
                                               "origin": origin, "indices": set(), "verdicts": []})
            bucket["indices"].add(test["index"])
    errors = [row for row in rows if row["index"] == 0]
    scopes = []
    for bucket in buckets.values():
        observed = [row for row in rows if row["testset"] == bucket["testset"] and row["index"] in bucket["indices"]]
        # Do not evaluate unrequested groups as if they had run.
        requested = {test["index"] for test in prepared[bucket["testset"]]}
        if not bucket["indices"] & requested:
            continue
        verdicts = [row["verdict"] for row in observed]
        tag = bucket["tag"]
        complete = tag == "NR" or {row["index"] for row in observed} == bucket["indices"]
        matched = matches(tag, verdicts) if complete or verdicts else None
        if not complete and matched is False and tag not in {"MA", "OK", "TO"} and not set(verdicts) & {"CE", "JUDGE_ERROR"}:
            matched = None
        scopes.append({key: value for key, value in bucket.items() if key not in {"indices", "verdicts"}} | {
            "tested": len(observed), "total": len(bucket["indices"]), "complete": complete,
            "matched": matched, "skipped": tag == "NR", "verdicts": sorted(set(verdicts)),
        })
    matched = bool(scopes) and not errors and all(scope["matched"] is not False for scope in scopes)
    return {"matched": matched, "expectations": scopes,
            "expectations_complete": bool(scopes) and not errors and all(scope["complete"] for scope in scopes)}
