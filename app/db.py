import uuid
from datetime import datetime, timezone
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from .config import get_settings


def now() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def uid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    handle: Mapped[str] = mapped_column(String(40), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    central_account_id: Mapped[str | None] = mapped_column(String(36),nullable=True,unique=True,index=True)
    created_at: Mapped[int] = mapped_column(default=now)


class LoginCode(Base):
    __tablename__ = "login_codes"
    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64))
    requested_at: Mapped[int]
    expires_at: Mapped[int]
    attempts: Mapped[int] = mapped_column(default=0)
    window_started: Mapped[int]
    request_count: Mapped[int] = mapped_column(default=1)


class RateLimit(Base):
    __tablename__ = "rate_limits"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[int]
    count: Mapped[int] = mapped_column(default=1)


class LoginSession(Base):
    __tablename__ = "login_sessions"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[int] = mapped_column(index=True)


class Problem(Base):
    __tablename__ = "problems"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    slug: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(default=0)
    document: Mapped[dict] = mapped_column(JSON)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[int] = mapped_column(default=now)
    updated_at: Mapped[int] = mapped_column(default=now)
    __table_args__ = (UniqueConstraint("owner_id", "slug"),)


class Access(Base):
    __tablename__ = "accesses"
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="READ")
    reviewer: Mapped[bool] = mapped_column(default=False)
    translator: Mapped[bool] = mapped_column(default=False)


class Folder(Base):
    __tablename__ = "folders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), nullable=True)
    parent_key: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(80))
    name_key: Mapped[str] = mapped_column(String(240))
    __table_args__ = (UniqueConstraint("user_id", "parent_key", "name_key"),)


class Placement(Base):
    __tablename__ = "problem_placements"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True)
    folder_id: Mapped[str | None] = mapped_column(ForeignKey("folders.id", ondelete="SET NULL"), nullable=True, index=True)


class WorkingCopy(Base):
    __tablename__ = "working_copies"
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    base_revision: Mapped[int]
    base_document: Mapped[dict] = mapped_column(JSON)
    document: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[int] = mapped_column(default=now)


class Revision(Base):
    __tablename__ = "revisions"
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True)
    number: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    minor: Mapped[bool] = mapped_column(default=False)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[int] = mapped_column(default=now)


class Blob(Base):
    __tablename__ = "blobs"
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int]


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    document: Mapped[dict] = mapped_column(JSON)
    document_hash: Mapped[str] = mapped_column(String(64))
    workspace_version: Mapped[int]
    revision: Mapped[int]
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    lease: Mapped[str | None] = mapped_column(String(32), nullable=True)
    heartbeat_at: Mapped[int] = mapped_column(default=now)
    created_at: Mapped[int] = mapped_column(default=now)
    started_at: Mapped[int | None] = mapped_column(nullable=True)
    finished_at: Mapped[int | None] = mapped_column(nullable=True)


class JobApplication(Base):
    __tablename__ = "job_applications"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    applied_at: Mapped[int] = mapped_column(default=now)
    workspace_version: Mapped[int]
    document_hash: Mapped[str] = mapped_column(String(64))


class Issue(Base):
    __tablename__ = "issues"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20), default="BUG")
    status: Mapped[str] = mapped_column(String(20), default="OPENED")
    assignee_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    comments: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column(default=now)
    updated_at: Mapped[int] = mapped_column(default=now)


class Collection(Base):
    __tablename__ = "collections"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    problem_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[int] = mapped_column(default=now)


class Publication(Base):
    __tablename__ = "zoj_publications"
    operation_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    problem_id: Mapped[str] = mapped_column(ForeignKey("problems.id"), index=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    contest_id: Mapped[str] = mapped_column(String(36))
    division_id: Mapped[str] = mapped_column(String(36))
    problem_code: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[int] = mapped_column(default=now)


class LibraryNonce(Base):
    __tablename__ = "zoj_library_nonces"
    nonce: Mapped[str] = mapped_column(String(32), primary_key=True)
    expires_at: Mapped[int] = mapped_column(index=True)


settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False, "timeout": 15} if settings.database_url.startswith("sqlite") else {}, pool_pre_ping=True)
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def sqlite_connect(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def session_dependency():
    with SessionLocal() as db:
        yield db
