"""Durable folder snapshots coordinated through the existing package jobs."""
import copy
import hashlib
import json
import logging
import re
import shutil
import tempfile
import threading
import zipfile
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .auth import current_user
from .config import get_settings
from .db import Access, FolderPackageExport, Job, Placement, Problem, SharedPlacement, SessionLocal, User, now, session_dependency
from .domain import authorize, document_hash, fail
from .folders import accessible_folders, lock_user, owned_folder
from .folder_access import merge_access, problem_access_context, visible_problem_ids
from .jobs import JobOptions, JobRequest, _create_job
from .permissions import has
from .package_reuse import artifact_present_summary, cached_package, invalidate_artifact, package_path
from .schema import Strict

router = APIRouter(prefix="/api", tags=["folder package exports"])
logger = logging.getLogger("editor.folder_package_exports")
ACTIVE = {"queued", "running"}
MAX_EXPORT_PROBLEMS = 500
MAX_ACTIVE_EXPORTS = 2
MAX_ACTIVE_FOLDER_JOBS = 2
coordinator_idle = threading.Event()
coordinator_idle.set()


class ExportOptions(Strict):
    include_descendants: bool = True
    export_format: Literal["editor", "zoj", "linux"] = "zoj"
    verify: bool = True
    reuse: bool = True
    statement_language: str = Field(default="korean", min_length=1, max_length=100)


def component(value):
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value).strip(" .")
    return (value or "folder")[:80]


def error_json(error):
    return error.detail if isinstance(error.detail, dict) else {"code": "export_failed", "message": str(error.detail)}


def failed_item(item, error):
    item.update(status="failed", progress=100, error=error)


def permission_errors(db, user, items):
    """Batch live ACL checks without loading problem documents on each poll."""
    ids = [item["problem_id"] for item in items]
    available = set(db.scalars(select(Problem.id).where(Problem.id.in_(ids), Problem.deleted.is_(False))))
    direct, shared, members, _ = problem_access_context(db, ids, user.id)
    result = {}
    for problem_id in ids:
        folder = shared.get(problem_id)
        access = merge_access(direct.get(problem_id), members.get(folder.workspace_id) if folder else None)
        if problem_id not in available or not access:
            result[problem_id] = {"code": "not_found", "message": "문제를 찾을 수 없습니다."}
        elif not has(access, "package"):
            result[problem_id] = {"code": "forbidden", "message": "이 작업에 필요한 기능 권한이 없습니다.", "required_permissions": ["package"]}
    return result


def update_status(row):
    pending = sum(item["status"] not in {"ready", "failed"} for item in row.items)
    ready = sum(item["status"] == "ready" for item in row.items)
    failed = len(row.items) - pending - ready
    if pending:
        row.status = "running" if any(item.get("job_id") or item["status"] != "pending" for item in row.items) else "queued"
        row.finished_at = None
    else:
        row.status = "partial" if ready and failed else "completed" if ready else "failed"
        row.finished_at = row.finished_at or now()


def export_json(row, detail=True):
    ready = sum(item["status"] == "ready" for item in row.items)
    failed = sum(item["status"] == "failed" for item in row.items)
    value = {"id": row.id, "folder_id": row.folder_id, "folder_name": row.folder_name,
        "include_descendants": row.include_descendants, "options": row.options, "status": row.status,
        "created_at": row.created_at, "finished_at": row.finished_at, "total": len(row.items),
        "ready": ready, "failed": failed, "pending": len(row.items) - ready - failed,
        "progress": round(sum(item.get("progress", 0) for item in row.items) / len(row.items)) if row.items else 100,
        "download_url": f"/api/folder-package-exports/{row.id}/download" if ready and row.status not in ACTIVE else None}
    if detail:
        value["items"] = [{key: val for key, val in item.items() if key not in {"source_document_hash", "testsets", "document_testsets"}} for item in row.items]
    return value


def snapshot(db, user, root, recursive):
    folders = {folder.id: folder for folder in accessible_folders(db, user)}
    selected = {root.id}
    if recursive:
        while True:
            children = {folder.id for folder in folders.values() if folder.parent_id in selected}
            if children <= selected:
                break
            selected |= children
    shared = select(SharedPlacement.problem_id).where(SharedPlacement.folder_id.in_(selected))
    personal = select(Placement.problem_id).where(Placement.user_id == user.id, Placement.folder_id.in_(selected),
        ~select(SharedPlacement.problem_id).where(SharedPlacement.problem_id == Placement.problem_id).exists())
    ids = shared if root.workspace_id else personal
    visible = (Problem.id.in_(select(Access.problem_id).where(Access.user_id == user.id))) | (
        Problem.id.in_(visible_problem_ids(db, user.id)))
    problems = db.scalars(select(Problem).where(Problem.id.in_(ids), visible, Problem.deleted.is_(False))
        .order_by(Problem.id).limit(MAX_EXPORT_PROBLEMS + 1)).all()
    if not problems:
        fail("empty_folder", "이 폴더에 내보낼 문제가 없습니다.")
    if len(problems) > MAX_EXPORT_PROBLEMS:
        fail("folder_export_too_large", f"한 번에 {MAX_EXPORT_PROBLEMS}개까지 내보낼 수 있습니다. 하위 폴더를 나누어 선택하세요.")
    model = SharedPlacement if root.workspace_id else Placement
    placement_query = select(model).where(model.problem_id.in_([problem.id for problem in problems]))
    if model is Placement:
        placement_query = placement_query.where(Placement.user_id == user.id)
    placements = {placement.problem_id: placement.folder_id for placement in db.scalars(placement_query)}
    result = []
    for problem in problems:
        path = []
        folder = folders[placements[problem.id]]
        while True:
            path.append(folder)
            if folder.id == root.id:
                break
            folder = folders[folder.parent_id]
        path.reverse()
        item = {"problem_id": problem.id, "name": problem.document["info"]["name"], "slug": problem.slug,
            "revision": problem.revision, "folder_path": [folder.name for folder in path],
            "archive_directory": "/".join(component(folder.name) + "-" + folder.id[:8] for folder in path),
            "source_document_hash": document_hash(problem.document), "testsets": list(problem.document["testsets"]),
            "status": "pending", "job_id": None, "reused": False, "progress": 0, "error": None}
        try:
            authorize(db, problem.id, user, permission="package")
        except HTTPException as error:
            if error.status_code == 404:
                item["name"], item["slug"] = "접근할 수 없는 문제", problem.id[:8]
            failed_item(item, error_json(error))
        result.append(item)
    return result


@router.post("/folders/{folder_id}/package-exports", status_code=202)
def create_export(folder_id: str, values: ExportOptions, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    root = owned_folder(db, user, folder_id, write=False)
    # Shared roots can be exported by each other's owners. A global lock order
    # prevents opposite requester/owner pairs from deadlocking in PostgreSQL.
    for user_id in sorted({user.id, root.user_id}):
        lock_user(db, db.get(User, user_id))
    root = owned_folder(db, user, folder_id, write=False)
    items = snapshot(db, user, root, values.include_descendants)
    options = values.model_dump(exclude={"include_descendants"})
    for item in items:
        suffix = ".zoj.zip" if options["export_format"] == "zoj" else "-linux.zip" if options["export_format"] == "linux" else ".zip"
        item["archive_path"] = item.pop("archive_directory") + "/" + component(item["slug"]) + "-" + item["problem_id"] + f"-r{item['revision']}" + suffix
    fingerprint = document_hash({"folder_id": folder_id, "recursive": values.include_descendants,
        "options": options, "items": [{key: item[key] for key in ("problem_id", "revision", "source_document_hash", "archive_path")} for item in items]})
    active = db.scalars(select(FolderPackageExport).where(FolderPackageExport.user_id == user.id,
        FolderPackageExport.status.in_(ACTIVE)).order_by(FolderPackageExport.created_at.desc())).all()
    duplicate = next((row for row in active if row.fingerprint == fingerprint), None)
    if duplicate:
        return export_json(duplicate)
    if len(active) >= MAX_ACTIVE_EXPORTS:
        fail("too_many_folder_exports", f"폴더 내보내기는 동시에 {MAX_ACTIVE_EXPORTS}개까지 요청할 수 있습니다.", 429)
    row = FolderPackageExport(user_id=user.id, folder_id=root.id, folder_name=root.name,
        include_descendants=values.include_descendants, fingerprint=fingerprint, options=options, items=items)
    db.add(row)
    db.flush()
    update_status(row)
    db.commit()
    return export_json(row)


def refresh_export(db, row, user):
    items = copy.deepcopy(row.items)
    errors = permission_errors(db, user, items)
    ids = [item["job_id"] for item in items if item.get("job_id")]
    jobs = {job.id: job for job in db.execute(select(Job.id, Job.status,
        Job.result["summary"].as_string().label("summary"), Job.result["progress"].as_float().label("progress"),
        Job.result["package"].label("package")).where(Job.id.in_(ids)))}
    for item in items:
        if item["problem_id"] in errors:
            error = errors[item["problem_id"]]
            if error["code"] == "not_found":
                item.update(name="접근할 수 없는 문제", slug="", folder_path=[], archive_path="", job_id=None)
            failed_item(item, error)
            continue
        if item["status"] == "failed":
            continue
        if not item.get("job_id"):
            continue
        job = jobs.get(item["job_id"])
        if not job:
            failed_item(item, {"code": "package_job_missing", "message": "패키지 생성 작업을 찾을 수 없습니다."})
        elif job.status in ACTIVE:
            progress = job.progress or 0
            item.update(status=job.status, progress=max(0, min(99, int(progress))) if isinstance(progress, (int, float)) else 0)
        elif job.status == "completed" and artifact_present_summary(job.id, job.package):
            item.update(status="ready", progress=100, error=None)
        elif job.status == "completed":
            failed_item(item, {"code": "package_artifact_unavailable", "message": "저장된 패키지를 사용할 수 없습니다. 내보내기를 다시 요청하세요."})
        else:
            failed_item(item, {"code": "package_generation_failed", "message": job.summary or "패키지를 생성하지 못했습니다."})
    row.items = items
    update_status(row)


def own_export(db, user, export_id):
    row = db.scalar(select(FolderPackageExport).where(FolderPackageExport.id == export_id,
        FolderPackageExport.user_id == user.id).with_for_update())
    if not row:
        fail("folder_export_not_found", "폴더 내보내기 작업을 찾을 수 없습니다.", 404)
    owned_folder(db, user, row.folder_id, write=False)
    refresh_export(db, row, user)
    return row


@router.get("/folders/{folder_id}/package-exports")
def list_exports(folder_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    owned_folder(db, user, folder_id, write=False)
    rows = db.scalars(select(FolderPackageExport).where(FolderPackageExport.user_id == user.id,
        FolderPackageExport.folder_id == folder_id).order_by(FolderPackageExport.created_at.desc(), FolderPackageExport.id.desc()).limit(10).with_for_update()).all()
    for row in rows:
        refresh_export(db, row, user)
    db.commit()
    return {"items": [export_json(row, detail=False) for row in rows]}


@router.get("/folder-package-exports/{export_id}")
def get_export(export_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = own_export(db, user, export_id)
    db.commit()
    return export_json(row)


@router.get("/folder-package-exports/{export_id}/download")
def download_export(export_id: str, user: User = Depends(current_user), db: Session = Depends(session_dependency)):
    row = own_export(db, user, export_id)
    if row.status in ACTIVE:
        fail("folder_export_pending", "패키지를 준비하고 있습니다. 완료 후 내려받으세요.", 409)
    manifest = export_json(row)
    ids = [item["job_id"] for item in manifest["items"] if item["status"] == "ready"]
    artifacts = {job.id: job.package for job in db.execute(select(Job.id,
        Job.result["package"].label("package")).where(Job.id.in_(ids)))}
    folder_id, user_id = row.folder_id, user.id
    filename = f"folder-{folder_id[:8]}-packages.zip"
    db.commit()
    db.close()
    limit = get_settings().folder_export_max_archive_bytes
    if sum(artifact["size"] for artifact in artifacts.values()) > limit:
        fail("folder_export_archive_too_large", f"묶음 ZIP은 {limit / 1024 ** 3:g}GiB 이하여야 합니다. 하위 폴더를 나누어 내려받으세요.", 413)
    archive = tempfile.TemporaryFile()
    try:
        items = manifest["items"]
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as output:
            for item in items:
                if item["status"] != "ready":
                    continue
                # Re-read each inherited grant immediately before including its
                # private source package, including already cached artifacts.
                try:
                    with SessionLocal() as access_db:
                        actor = access_db.get(User, user_id)
                        owned_folder(access_db, actor, folder_id, write=False)
                        authorize(access_db, item["problem_id"], actor, permission="package")
                    source = package_path(item["job_id"]).open("rb")
                    try:
                        if hashlib.file_digest(source, "sha256").hexdigest() != artifacts[item["job_id"]]["sha256"]:
                            fail("package_artifact_corrupt", "저장된 패키지가 손상되었습니다. 내보내기를 다시 요청하세요.", 409)
                        source.seek(0)
                    except BaseException:
                        source.close()
                        raise
                except HTTPException as error:
                    failed_item(item, error_json(error))
                    continue
                except OSError:
                    failed_item(item, {"code": "package_artifact_missing", "message": "저장된 패키지 파일을 찾을 수 없습니다. 내보내기를 다시 요청하세요."})
                    continue
                # A copy/storage error aborts the response; never publish an
                # incomplete ZIP entry as if the problem had been omitted.
                with source, output.open(item["archive_path"], "w", force_zip64=True) as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
            # No database connection is held while reading or copying package
            # files. A final short transaction also catches mid-copy revocation.
            with SessionLocal() as final_db:
                actor = final_db.get(User, user_id)
                owned_folder(final_db, actor, folder_id, write=False)
                live_errors = permission_errors(final_db, actor, items)
                if any(item["status"] == "ready" and item["problem_id"] in live_errors for item in items):
                    fail("folder_export_access_changed", "문제 권한이 변경되었습니다. 작업 상태를 새로고침한 뒤 다시 내려받으세요.", 409)
                persisted = final_db.scalar(select(FolderPackageExport).where(FolderPackageExport.id == export_id).with_for_update())
                invalid_ids = [item["job_id"] for item in items if item.get("job_id")
                    and (item.get("error") or {}).get("code") == "package_artifact_corrupt"]
                for job_id in invalid_ids:
                    # Preserve execution evidence while preventing future
                    # exports from selecting an artifact whose hash failed.
                    invalidate_artifact(final_db, job_id)
                internal = copy.deepcopy(persisted.items)
                outcomes = {item["problem_id"]: item for item in items}
                for item in internal:
                    outcome = outcomes[item["problem_id"]]
                    if outcome["status"] == "failed":
                        failed_item(item, outcome["error"])
                persisted.items = internal
                refresh_export(final_db, persisted, actor)
                manifest = export_json(persisted)
                final_db.commit()
            if not manifest["ready"]:
                fail("folder_export_no_packages", "내려받을 수 있는 패키지가 없습니다. 문제별 오류를 확인하세요.", 409)
            output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        length = archive.tell()
        archive.seek(0)
    except BaseException:
        archive.close()
        raise

    def chunks():
        try:
            while chunk := archive.read(1024 * 1024):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Content-Length": str(length),
    }, background=BackgroundTask(archive.close))


def active_folder_jobs(db, user_id):
    return db.scalars(select(Job.id).where(Job.user_id == user_id, Job.status.in_(ACTIVE),
        Job.options["folder_export_id"].as_string().is_not(None))).all()


_CAPABILITIES_NEEDED = object()


def advance_export(export_id, user_id, caps, draining):
    """Resolve existing artifacts before requesting any execution admission."""
    needs_caps = False
    with SessionLocal() as db:
        user = db.get(User, user_id)
        lock_user(db, user)
        row = db.scalar(select(FolderPackageExport).where(FolderPackageExport.id == export_id,
            FolderPackageExport.status.in_(ACTIVE)).with_for_update(skip_locked=True))
        if not row:
            return False
        row.last_coordinated_at = now()
        try:
            owned_folder(db, user, row.folder_id, write=False)
        except HTTPException as error:
            items = copy.deepcopy(row.items)
            for item in items:
                failed_item(item, error_json(error))
            row.items = items
            update_status(row)
            db.commit()
            return False
        refresh_export(db, row, user)
        active_children = active_folder_jobs(db, user_id)
        items = copy.deepcopy(row.items)
        examined = 0
        for item in items:
            if item["status"] != "pending":
                continue
            if examined >= 20 or draining():
                break
            examined += 1
            try:
                authorize(db, item["problem_id"], user, permission="package")
                request_options = {key: value for key, value in row.options.items() if key != "reuse"}
                request_options["testsets"] = item["testsets"] if row.options["export_format"] == "zoj" and item["testsets"] else None
                cached = cached_package(db, user, item, request_options) if row.options.get("reuse", True) else None
                if cached:
                    item.update(job_id=cached.id, reused=True, status="ready" if cached.status == "completed" else cached.status,
                        progress=100 if cached.status == "completed" else 0)
                    continue
                if len(active_children) >= MAX_ACTIVE_FOLDER_JOBS or draining():
                    break
                if row.options["verify"] and caps is _CAPABILITIES_NEEDED:
                    needs_caps = True
                    break
                if isinstance(caps, HTTPException):
                    raise caps
                options = JobOptions(**request_options, testset=item["testsets"][0] if item["testsets"] else "tests")
                with db.begin_nested():
                    created = _create_job(item["problem_id"], JobRequest(kind="package", version=0,
                        revision=item["revision"], options=options, reuse=False), user, db,
                        from_committed_revision=True, commit=False, capabilities_snapshot=caps, folder_export_id=row.id)
                item.update(job_id=created["id"], status=created["status"], progress=0)
                break
            except HTTPException as error:
                if error.status_code == 429:
                    break
                failed_item(item, error_json(error))
        row.items = items
        update_status(row)
        db.commit()
    return needs_caps


def advance_exports(draining=lambda: False):
    """One child per export per pass; network calls never hold row locks."""
    with SessionLocal() as db:
        candidates = db.execute(select(FolderPackageExport.id, FolderPackageExport.user_id)
            .where(FolderPackageExport.status.in_(ACTIVE))
            .order_by(FolderPackageExport.last_coordinated_at, FolderPackageExport.created_at, FolderPackageExport.id).limit(20)).all()
    capabilities = {}
    for export_id, user_id in candidates:
        if draining():
            return
        caps = capabilities.get(user_id, _CAPABILITIES_NEEDED)
        if not advance_export(export_id, user_id, caps, draining):
            continue
        if draining():
            return
        with SessionLocal() as db:
            user = db.get(User, user_id)
        if draining():
            return
        try:
            from .judge_bridge import capabilities as get_capabilities
            capabilities[user_id] = get_capabilities(user)
        except HTTPException as error:
            capabilities[user_id] = error
        if draining():
            return
        advance_export(export_id, user_id, capabilities[user_id], draining)


def run_coordinator(stop_event, drain_event):
    while not stop_event.is_set():
        try:
            if not drain_event.is_set():
                coordinator_idle.clear()
                if not drain_event.is_set():
                    advance_exports(draining=lambda: stop_event.is_set() or drain_event.is_set())
        except Exception:
            logger.exception("Folder package export coordinator failed")
        finally:
            coordinator_idle.set()
        stop_event.wait(1)
