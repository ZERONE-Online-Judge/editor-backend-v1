"""Durable, retryable notifications for new invitations and added permissions."""
import logging
from sqlalchemy import delete, select, update
from .db import Access, AccessMail, Folder, Problem, SessionLocal, User, WorkspaceInvitation, WorkspaceMember, now, uid
from .permissions import grants
from .folder_access import workspace_access

logger = logging.getLogger("editor.access_mail")


def enqueue(db, *, kind, resource_id, resource_name, email, actor, permissions, invitation_id=None, expires_at=None):
    # Deliberately do not commit here: a rolled-back grant must not send mail.
    # Re-inviting after revocation must not revive an older unsent invitation.
    db.execute(update(AccessMail).where(AccessMail.kind == kind,
        AccessMail.resource_id == resource_id, AccessMail.recipient_email == email.lower(),
        AccessMail.status.in_(["pending", "sending"])).values(status="cancelled", lease=None))
    db.add(AccessMail(kind=kind, resource_id=resource_id, resource_name=" ".join(resource_name.split())[:200] or "문제",
        recipient_email=email.lower(), actor_name=" ".join(actor.display_name.split())[:80] or "운영자",
        permissions=sorted(permissions), invitation_id=invitation_id, expires_at=expires_at))


def current_grant(db, row):
    target = db.scalar(select(User).where(User.email == row.recipient_email))
    if row.kind == "problem_access":
        problem = db.get(Problem, row.resource_id)
        access = db.get(Access, (row.resource_id, target.id)) if target else None
        return bool(problem and not problem.deleted and access and grants(access) == set(row.permissions))
    folder = db.get(Folder, row.resource_id)
    if not folder:
        return False
    if row.kind == "folder_invitation":
        invite = db.get(WorkspaceInvitation, row.invitation_id)
        inviter = workspace_access(db, row.resource_id, invite.inviter_id) if invite else None
        return bool(invite and invite.workspace_id == row.resource_id and invite.email == row.recipient_email
                    and invite.status == "pending" and invite.expires_at > now() and row.expires_at > now()
                    and grants(invite, folder=True) == set(row.permissions)
                    and "manage_access" in grants(inviter, folder=True)
                    and set(row.permissions) <= grants(inviter, folder=True))
    member = db.get(WorkspaceMember, (row.resource_id, target.id)) if target else None
    return bool(member and grants(member, folder=True) == set(row.permissions))


def dispatch_one():
    at, lease = now(), uid()
    with SessionLocal() as db:
        row = db.scalar(select(AccessMail).where(AccessMail.status.in_(["pending", "sending"]),
            AccessMail.available_at <= at).order_by(AccessMail.available_at, AccessMail.id)
            .limit(1).with_for_update(skip_locked=True))
        if not row:
            return False
        changed = db.execute(update(AccessMail).where(AccessMail.id == row.id,
            AccessMail.status.in_(["pending", "sending"]), AccessMail.available_at <= at)
            .values(status="sending", lease=lease, available_at=at + 120, attempts=AccessMail.attempts + 1))
        if not changed.rowcount:
            db.rollback()
            return False
        db.refresh(row)
        if at - row.created_at >= 7 * 86400 or not current_grant(db, row):
            row.status, row.lease = "cancelled", None
            db.commit()
            return True
        payload = {"event_id": row.id, "kind": row.kind, "resource_id": row.resource_id,
            "resource_name": row.resource_name, "actor_name": row.actor_name, "permissions": row.permissions}
        if row.kind == "folder_invitation":
            payload["expires_at"] = row.expires_at
        ident, email, attempts = row.id, row.recipient_email, row.attempts
        db.commit()
    try:
        from .mail_relay import send_access
        delivery_id = send_access(email, payload)
    except Exception as error:
        # Never persist/log transport bodies, credentials or recipient addresses.
        logger.warning("Access notification %s delivery failed (%s)", ident, type(error).__name__)
        values = {"status": "pending" if attempts < 20 else "failed", "lease": None,
            "available_at": now() + min(3600, 15 * 2 ** min(attempts - 1, 8)), "last_error": type(error).__name__[:80]}
    else:
        values = {"status": "queued", "lease": None, "delivery_id": delivery_id, "last_error": None}
    with SessionLocal() as db:
        db.execute(update(AccessMail).where(AccessMail.id == ident, AccessMail.lease == lease).values(**values))
        db.commit()
    return True


def run(stop_event):
    while not stop_event.is_set():
        try:
            if dispatch_one():
                continue
        except Exception:
            logger.error("Access notification worker failed; retrying")
        stop_event.wait(1)


def cleanup(db):
    db.execute(delete(AccessMail).where(AccessMail.created_at < now() - 30 * 86400))
