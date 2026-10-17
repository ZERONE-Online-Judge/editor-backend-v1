"""One capability vocabulary for direct sharing and inherited team access.

NULL preserves legacy roles. An explicit empty grant means read-only, never
fallback to a role. Owners are protected and always retain every capability.
"""
from typing import Literal

Permission = Literal["edit_info", "edit_statements", "edit_files", "edit_solutions",
    "edit_tests", "edit_materials", "translate", "execute", "render", "prepare",
    "package", "publish", "issues", "manage_access", "manage_folders", "create_problems"]
EDIT = {"edit_info", "edit_statements", "edit_files", "edit_solutions", "edit_tests", "edit_materials"}
PROBLEM = EDIT | {"translate", "execute", "render", "prepare", "package", "publish", "issues", "manage_access"}
ALL = PROBLEM | {"manage_folders", "create_problems"}
WRITER = PROBLEM - {"translate"}
PRESETS = {"OWNER": ALL, "ADMIN": ALL, "EDITOR": WRITER - {"manage_access"} | {"manage_folders", "create_problems"},
           "REVIEWER": {"execute", "render", "issues"}, "TRANSLATOR": {"translate", "render", "issues"}, "VIEWER": set()}


def grants(row, *, folder=False):
    if not row:
        return set()
    if isinstance(row, str):
        return set(PRESETS.get(row, set()))
    if row.role == "OWNER":
        return set(ALL if folder else PROBLEM)
    explicit = getattr(row, "permissions", None)
    if explicit is not None:
        return set(explicit) & (ALL if folder else PROBLEM)
    if folder:
        return set(PRESETS.get(row.role, set()))
    result = set(WRITER if row.role == "WRITE" else ())
    if getattr(row, "reviewer", False):
        result |= PRESETS["REVIEWER"]
    if getattr(row, "translator", False):
        result |= PRESETS["TRANSLATOR"]
    return result


def has(access, permission):
    return permission in grants(access)


def editing(access):
    return bool(grants(access) & (EDIT | {"translate"}))


def require(access, *permissions):
    from .domain import fail
    if not set(permissions) <= grants(access):
        fail("forbidden", "이 작업에 필요한 기능 권한이 없습니다.", 403, required_permissions=list(permissions))


def delegate(actor, requested, *, folder=False):
    from .domain import fail
    allowed = grants(actor, folder=folder)
    if not set(requested) <= allowed:
        fail("permission_escalation", "본인이 가진 권한만 다른 사람에게 부여할 수 있습니다.", 403)


def stored(values):
    return {key: True for key in sorted(values)}


def projection(values):
    return "WRITE" if EDIT <= set(values) else "READ"


def job_permission(kind):
    return {"prepare": "prepare", "package": "package", "render": "render"}.get(kind, "execute")


def job_allowed(access, job):
    if not set(job.options.get("permission_requirements", [])) <= grants(access):
        return False
    if not has(access, job_permission(job.kind)):
        return False
    if job.options.get("auto_apply") and not has(access, "edit_tests"):
        return False
    # A reviewer must not execute a stale personal draft after losing edit access.
    return job.options.get("snapshot", "working_copy") != "working_copy" or editing(access)


FIELDS = {"info": "edit_info", "statements": "edit_statements", "common_statement": "edit_statements",
    "common_tutorial": "edit_statements", "translation_source": "edit_statements", "translation_reviews": "edit_statements",
    "files": "edit_files", "checker": "edit_files", "validator": "edit_files", "extra_validators": "edit_files",
    "interactor": "edit_files", "validator_tests": "edit_files", "checker_tests": "edit_files",
    "solutions": "edit_solutions", "testsets": "edit_tests", "materials": "edit_materials"}


def check_changes(access, before, after):
    from .domain import fail
    for key in set(before) | set(after):
        if before.get(key) == after.get(key) or has(access, FIELDS.get(key, "")):
            continue
        if has(access, "translate") and key in {"statements", "translation_reviews"}:
            basis = before.get("translation_source")
            if key != "statements" or not basis or before.get(key, {}).get(basis) == after.get(key, {}).get(basis):
                continue
        code = "translator_scope" if has(access, "translate") else "forbidden"
        fail(code, "수정 권한이 없는 문제 항목이 포함되어 있습니다.", 403, field=key)
