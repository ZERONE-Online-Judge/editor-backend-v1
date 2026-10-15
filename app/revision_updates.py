"""Watch committed metadata without loading documents or holding DB connections."""
import asyncio
from time import monotonic

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import exists, or_, select
from starlette.concurrency import run_in_threadpool

from .auth import current_user
from .db import Access, Problem, Revision, SessionLocal, User
from .domain import fail
from .folder_access import visible_problem_ids

router = APIRouter(prefix="/api/problems", tags=["revision updates"])
POLL_SECONDS = 1.0


def viewer_id(request):
    with SessionLocal() as db:
        return current_user(request, db).id


def revision_status(problem_id, user_id):
    with SessionLocal() as db:
        direct = exists(select(Access.problem_id).where(Access.problem_id == Problem.id, Access.user_id == user_id))
        row = db.execute(select(Problem.revision, Revision.message, Revision.created_at, User.display_name)
            .select_from(Problem)
            .outerjoin(Revision, (Revision.problem_id == Problem.id) & (Revision.number == Problem.revision))
            .outerjoin(User, User.id == Revision.author_id)
            .where(Problem.id == problem_id, Problem.deleted.is_(False),
                   or_(direct, Problem.id.in_(visible_problem_ids(db, user_id))))).first()
        if row is None:
            fail("not_found", "문제를 찾을 수 없습니다.", 404)
        return {"revision": row.revision, "author": row.display_name,
                "message": (row.message or "")[:240], "created_at": row.created_at}


async def wait_for_revision(request, problem_id, user_id, after, wait_seconds):
    deadline = monotonic() + wait_seconds
    status = await run_in_threadpool(revision_status, problem_id, user_id)
    if after is None or status["revision"] > after or wait_seconds == 0:
        return status
    while monotonic() < deadline:
        if await request.is_disconnected():
            return Response(status_code=204)
        await asyncio.sleep(min(POLL_SECONDS, max(0, deadline - monotonic())))
        status = await run_in_threadpool(revision_status, problem_id, user_id)
        if status["revision"] > after:
            break
    # The request can outlive a login or an ACL grant. Revalidate before sending
    # metadata; every poll also checks the current direct and folder grants.
    if await run_in_threadpool(viewer_id, request) != user_id:
        fail("authentication_required", "로그인이 필요합니다.", 401)
    return await run_in_threadpool(revision_status, problem_id, user_id)


@router.get("/{problem_id}/revision-status")
async def watch_revision(problem_id: str, request: Request,
                         after: int | None = Query(default=None, ge=0),
                         wait_seconds: float = Query(default=25, ge=0, le=25, allow_inf_nan=False)):
    user_id = await run_in_threadpool(viewer_id, request)
    return await wait_for_revision(request, problem_id, user_id, after, wait_seconds)
