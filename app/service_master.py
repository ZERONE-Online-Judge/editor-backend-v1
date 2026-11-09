"""Service-master authoring metadata without granting access to problem data."""

from collections import defaultdict
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, literal, or_, select, union_all
from sqlalchemy.orm import Session, aliased

from .auth import require_service_master
from .db import (Access, Folder, Problem, Revision, SharedPlacement,
                 SharedWorkspace, User, WorkingCopy, WorkspaceMember,
                 session_dependency)
from .domain import fail
from .folder_access import merge_access
from .pagination import page_rows
from .permissions import editing

router = APIRouter(prefix="/api/service-master", tags=["service master"],
                   dependencies=[Depends(require_service_master)])


def person(user):
    return {"id": user.id, "email": user.email, "handle": user.handle,
            "display_name": user.display_name} if user else None


def activity_query():
    # Select metadata only: neither historical nor personal documents are read.
    events = union_all(
        select(Problem.id.label("problem_id"), Problem.created_at.label("at"),
               Problem.owner_id.label("actor_id"), literal("created").label("kind"),
               literal(0).label("priority"), literal(0).label("sequence")),
        select(Revision.problem_id, Revision.created_at, Revision.author_id,
               literal("commit"), literal(2), Revision.number).where(Revision.number > 0),
        select(WorkingCopy.problem_id, WorkingCopy.updated_at, WorkingCopy.user_id,
               literal("working_copy"), literal(1), WorkingCopy.version).where(WorkingCopy.version > 0),
    ).subquery()
    ranked = select(events, func.row_number().over(
        partition_by=events.c.problem_id,
        order_by=(events.c.at.desc(), events.c.priority.desc(),
                  events.c.sequence.desc(), events.c.actor_id.asc()),
    ).label("position")).subquery()
    return select(ranked).where(ranked.c.position == 1).subquery()


def metadata_query():
    owner = aliased(User)
    activity = activity_query()
    return select(
        Problem.id, Problem.slug, Problem.document["info"]["name"].as_string().label("name"),
        Problem.deleted, Problem.created_at, Problem.updated_at, Problem.revision,
        owner.id.label("owner_id"), owner.email.label("owner_email"),
        owner.handle.label("owner_handle"), owner.display_name.label("owner_name"),
        activity.c.at.label("last_activity_at"), activity.c.kind.label("last_activity_type"),
        activity.c.actor_id.label("last_actor_id"),
    ).join(owner, owner.id == Problem.owner_id).join(activity, activity.c.problem_id == Problem.id)


def editor_metadata(db, problem_ids):
    """Resolve current direct and inherited editors in a fixed number of reads."""
    if not problem_ids:
        return {}, {}
    direct = defaultdict(dict)
    for access in db.scalars(select(Access).where(Access.problem_id.in_(problem_ids))):
        direct[access.problem_id][access.user_id] = access
    placements = dict(db.execute(select(SharedPlacement.problem_id, Folder.workspace_id)
        .join(Folder, Folder.id == SharedPlacement.folder_id)
        .where(SharedPlacement.problem_id.in_(problem_ids))).all())
    workspace_ids = {ident for ident in placements.values() if ident}
    members = defaultdict(dict)
    if workspace_ids:
        for member in db.scalars(select(WorkspaceMember).where(WorkspaceMember.workspace_id.in_(workspace_ids))):
            members[member.workspace_id][member.user_id] = member
        for ident, owner in db.execute(select(SharedWorkspace.id, SharedWorkspace.owner_id)
                .where(SharedWorkspace.id.in_(workspace_ids))):
            members[ident][owner] = SimpleNamespace(role="OWNER", permissions=None)
    copies = {(pid, uid): (saved if version > 0 else None, revision)
              for pid, uid, saved, revision, version in db.execute(select(
                  WorkingCopy.problem_id, WorkingCopy.user_id, WorkingCopy.updated_at,
                  WorkingCopy.base_revision, WorkingCopy.version)
                  .where(WorkingCopy.problem_id.in_(problem_ids)))}
    editors = {}
    for pid in problem_ids:
        inherited = members.get(placements.get(pid), {})
        actors = []
        for uid in set(direct[pid]) | set(inherited):
            access = merge_access(direct[pid].get(uid), inherited.get(uid))
            if editing(access):
                saved, revision = copies.get((pid, uid), (None, None))
                actors.append({"user_id": uid, "role": access.role,
                               "last_saved_at": saved, "working_revision": revision})
        editors[pid] = actors
    return editors, copies


def build_items(db, rows, editors):
    actor_ids = {row.last_actor_id for row in rows}
    actor_ids.update(editor["user_id"] for people in editors.values() for editor in people)
    users = {user.id: user for user in db.scalars(select(User).where(User.id.in_(actor_ids)))} if actor_ids else {}
    items = []
    for row in rows:
        owner = {"id": row.owner_id, "email": row.owner_email,
                 "handle": row.owner_handle, "display_name": row.owner_name}
        items.append({"id": row.id, "slug": row.slug, "name": row.name,
            "deleted": row.deleted, "owner": owner, "created_at": row.created_at,
            "updated_at": row.updated_at, "revision": row.revision,
            "last_activity_at": row.last_activity_at,
            "last_activity_type": row.last_activity_type,
            "last_actor": person(users.get(row.last_actor_id)),
            "editor_count": len(editors.get(row.id, []))})
    return items, users


@router.get("/problems")
def list_problems(q: str = Query(default="", max_length=200), deleted: bool = False,
        sort: Literal["recent", "created", "name", "owner"] = "recent",
        page: int = Query(default=1, ge=1, le=1000000),
        page_size: int = Query(default=10, ge=1, le=50),
        db: Session = Depends(session_dependency)):
    metadata = metadata_query().where(Problem.deleted == deleted).subquery()
    query = select(metadata)
    if term := q.strip().lower():
        query = query.where(or_(*(func.lower(metadata.c[key]).contains(term, autoescape=True)
            for key in ("name", "slug", "owner_email", "owner_name", "owner_handle"))))
    order = {"recent": metadata.c.last_activity_at.desc(), "created": metadata.c.created_at.desc(),
             "name": func.lower(metadata.c.name).asc(), "owner": func.lower(metadata.c.owner_name).asc()}[sort]
    rows, paging = page_rows(db, query.order_by(order, metadata.c.id.asc()), page, page_size)
    editors, _ = editor_metadata(db, [row.id for row in rows])
    items, _ = build_items(db, rows, editors)
    return {"items": items, **paging}


@router.get("/problems/{problem_id}")
def problem_detail(problem_id: str, db: Session = Depends(session_dependency)):
    row = db.execute(metadata_query().where(Problem.id == problem_id)).one_or_none()
    if not row:
        fail("not_found", "문제를 찾을 수 없습니다.", 404)
    editors, _ = editor_metadata(db, [problem_id])
    items, users = build_items(db, [row], editors)
    actors = [{"user": person(users.get(editor["user_id"])),
               **{key: editor[key] for key in ("role", "last_saved_at", "working_revision")}}
              for editor in editors[problem_id]]
    actors.sort(key=lambda actor: (actor["role"] != "OWNER", actor["user"]["display_name"].lower(), actor["user"]["id"]))
    commit = db.execute(select(Revision.number, Revision.created_at, User.id, User.email,
        User.handle, User.display_name).join(User, User.id == Revision.author_id)
        .where(Revision.problem_id == problem_id).order_by(Revision.number.desc()).limit(1)).one_or_none()
    return {"problem": items[0], "editors": actors,
        "last_commit": {"number": commit.number, "created_at": commit.created_at,
                        "author": person(commit)} if commit else None}
