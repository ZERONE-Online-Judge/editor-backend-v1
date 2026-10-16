import copy
import logging
import signal
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import delete, select, update
from .config import get_settings
from .db import Access, Base, Job, LoginCode, LoginSession, RateLimit, SessionLocal, User, engine, now, uid
from .pipeline import Pipeline
from .folder_access import effective_access

logger = logging.getLogger("editor.worker")
stop_event = threading.Event()
drain_event = threading.Event()
DRAIN_MARKER = Path("/tmp/editor-worker-drained")


class Cancelled(RuntimeError):
    pass


def claim_job():
    with SessionLocal() as db:
        row = db.scalar(select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1).with_for_update(skip_locked=True))
        if not row:
            return None
        lease = uid()
        updated = db.execute(update(Job).where(Job.id == row.id, Job.status == "queued").values(status="running", lease=lease, started_at=now(), heartbeat_at=now()))
        if not updated.rowcount:
            db.rollback()
            return None
        db.commit()
        db.refresh(row)
        return row


def process_job(job):
    latest = {"summary": "검증 워커가 작업을 시작했습니다.", "progress": 0}
    finished = threading.Event()
    lock = threading.Lock()
    def check():
        with SessionLocal() as db:
            state = db.get(Job, job.id)
            if stop_event.is_set() or not state or state.status != "running" or state.lease != job.lease:
                raise Cancelled("작업이 취소되거나 워커가 중지되었습니다.")
            access = effective_access(db, job.problem_id, job.user_id)
            mode = job.options.get("permission_mode", "write")
            if not access or not (access.role in {"WRITE", "OWNER"} or mode == "reviewer" and access.reviewer or mode == "translator" and job.kind == "render" and access.translator):
                raise Cancelled("문제 실행 권한이 해제되었습니다.")
        if now() - job.started_at > get_settings().job_timeout_seconds:
            raise RuntimeError("작업 실행 한도에 도달했습니다.")
    def publish(result):
        nonlocal latest
        with lock:
            latest = copy.deepcopy(result)
    def heartbeat():
        while not finished.wait(3):
            with lock:
                value = copy.deepcopy(latest)
            with SessionLocal() as db:
                db.execute(update(Job).where(Job.id == job.id, Job.status == "running", Job.lease == job.lease).values(heartbeat_at=now(), result=value))
                db.commit()
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    status = "completed"
    pipeline = None
    try:
        pipeline = Pipeline(job, publish, check)
        latest = pipeline.run()
    except Cancelled as error:
        status = "cancelled"
        latest = copy.deepcopy(getattr(pipeline, "result", latest))
        latest = {**latest, "summary": str(error)}
    except Exception as error:
        status = "failed"
        latest = copy.deepcopy(getattr(pipeline, "result", latest))
        latest = {**latest, "summary": "작업을 완료하지 못했습니다.", "error": str(error)[:32768]}
        logger.exception("Editor job %s failed", job.id)
    finally:
        finished.set()
        thread.join(timeout=5)
    with SessionLocal() as db:
        updated = db.execute(update(Job).where(Job.id == job.id, Job.status == "running", Job.lease == job.lease).values(status=status, result=latest, heartbeat_at=now(), finished_at=now()))
        if not updated.rowcount:
            # A cancellation request has already changed status. Preserve the
            # same worker's final evidence without undoing that cancellation.
            db.execute(update(Job).where(Job.id == job.id, Job.status == "cancelled", Job.lease == job.lease)
                       .values(result=latest, heartbeat_at=now()))
        db.commit()
        if updated.rowcount and status == "completed" and job.kind == "prepare" and job.options.get("auto_apply"):
            from fastapi import HTTPException
            from .prepared_results import apply_prepared_results
            row = db.get(Job, job.id)
            try:
                apply_prepared_results(db, row, db.get(User, job.user_id), job.workspace_version)
                outcome = {"status": "applied"}
            except HTTPException as error:
                db.rollback()
                outcome = {"status": "blocked", **error.detail}
            except Exception:
                db.rollback()
                logger.exception("Auto-apply failed for Editor job %s", job.id)
                outcome = {"status": "blocked", "message": "자동 반영에 실패했습니다. 결과를 확인한 뒤 직접 반영하세요."}
            db.execute(update(Job).where(Job.id == job.id, Job.status == "completed", Job.lease == job.lease)
                       .values(result={**latest, "auto_apply": outcome}))
            db.commit()


def maintenance():
    with SessionLocal() as db:
        from .preparation_cache import expire_entries
        expire_entries(db)
        db.execute(update(Job).where(Job.status == "running", Job.heartbeat_at < now() - 120).values(status="failed", result={"summary": "검증 워커와의 연결이 중단되었습니다. 작업을 다시 요청하세요."}, finished_at=now()))
        db.execute(delete(LoginSession).where(LoginSession.expires_at < now()))
        db.execute(delete(LoginCode).where(LoginCode.expires_at < now() - 3600))
        db.execute(delete(RateLimit).where(RateLimit.started_at < now() - 86400))
        db.commit()


def main():
    logging.basicConfig(level=logging.INFO)
    if get_settings().env != "production":
        Base.metadata.create_all(engine)
    for sig in [signal.SIGTERM, signal.SIGINT]:
        signal.signal(sig, lambda *_: stop_event.set())
    signal.signal(signal.SIGUSR1, lambda *_: drain_event.set())
    DRAIN_MARKER.unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=get_settings().worker_concurrency) as pool:
        futures = set()
        last_maintenance = 0
        while not stop_event.is_set():
            futures = {f for f in futures if not f.done()}
            if drain_event.is_set() and not futures:
                DRAIN_MARKER.touch()
            if time.monotonic() - last_maintenance > 30:
                maintenance()
                last_maintenance = time.monotonic()
            if not drain_event.is_set() and len(futures) < get_settings().worker_concurrency:
                row = claim_job()
                if row:
                    futures.add(pool.submit(process_job, row))
                    continue
            stop_event.wait(0.5)


if __name__ == "__main__":
    main()
