"""Bounded, deterministic pages for growing activity histories."""
from fastapi import Query
from sqlalchemy import func, select


def page_rows(db, query, page, page_size):
    total = db.scalar(select(func.count()).select_from(query.order_by(None).subquery())) or 0
    pages = max(1, (total + page_size - 1) // page_size)
    current = min(page, pages)
    rows = db.execute(query.offset((current - 1) * page_size).limit(page_size)).all()
    return rows, {"total": total, "page": current, "page_size": page_size, "pages": pages}


def page_number():
    return Query(default=None, ge=1, le=1000000)


def page_length():
    return Query(default=20, ge=1, le=100)


def actor_json(user):
    return {"id": user.id, "display_name": user.display_name, "handle": user.handle} if user else None
