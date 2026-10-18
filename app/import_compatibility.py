"""Offer explicit language-setting changes without rewriting imported code."""
from fastapi import HTTPException


def language_plan(doc):
    from .judge_bridge import compatible
    try:
        compatible(doc, {"solutions": []}, "verify")
        return {"changes": [], "issues": []}
    except HTTPException as error:
        if not isinstance(error.detail, dict) or error.detail.get("code") != "judge_incompatible":
            raise
        details = error.detail
    labels = {row["language"]: row["label"] for row in details["supported_languages"]}
    replacements = {"cpp20": "cpp17", "c17": "c99", "java17": "java8", "pypy3": "python3"}
    changes = []
    for kind, rows in [("solution", details["unsupported_solutions"]),
                       ("checker", [details["unsupported_checker"]] if details.get("unsupported_checker") else [])]:
        for row in rows:
            target = replacements.get(row["language"])
            if kind == "checker" and target not in {"cpp17", "c99", "python3"}:
                target = None
            changes.append({"kind": kind, "name": row["name"], "from_language": row["language"],
                            "from_label": row["label"], "to_language": target,
                            "to_label": labels.get(target)})
    checker = details.get("unsupported_checker")
    issues = [issue for issue in details["issues"]
              if not checker or not issue.startswith(f"Checker {checker['name']}의 현재 언어는 ")]
    return {"changes": changes, "issues": issues}
