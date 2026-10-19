from .file_limits import MAX_FILE_BYTES
from pathlib import Path
from functools import lru_cache
from pydantic import Field, SecretStr, field_validator, model_validator
from urllib.parse import urlsplit
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDITOR_", env_file=".env", extra="ignore")
    env: str = "development"
    database_url: str = "sqlite:///./data/editor.db"
    data_dir: Path = Path("data")
    work_dir: Path = Path("data/work")
    public_origin: str = "http://localhost:5190"
    session_secret: str = "local-only-editor-development-secret-change-in-production"
    session_days: int = 7
    mail_mode: str = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = "ZOJ Editor <noreply@editor.zoj.kr>"
    runner_image: str = "zoj-editor-runtime:v1"
    render_image: str = "zoj-editor-renderer:v1"
    execution_transport: Literal["local", "agent"] | None = None
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    test_parallelism: int = Field(default=4, ge=1, le=16)
    execution_concurrency: int = Field(default=4, ge=1, le=16)
    job_timeout_seconds: int = Field(default=1800, ge=1, le=86400)
    stress_max_seconds: int = Field(default=1800, ge=1, le=86400)
    runtime_revision: str = "editor-runtime-0.2.0-icpc"
    # Retain old environment compatibility; new online judging is always ZOJ.
    judge_backend: Literal["editor", "zoj"] = "editor"
    judge_concurrency: int = 3
    judge_request_retry_seconds: float = Field(default=120, ge=1, le=600)
    max_project_bytes: int = 8 * 1024 * 1024 * 1024
    max_uncompressed_archive_bytes: int = 16 * 1024 * 1024 * 1024
    max_upload_bytes: int = Field(default=MAX_FILE_BYTES, ge=1, le=MAX_FILE_BYTES)
    max_archive_bytes: int = Field(default=2 * 1024 * 1024 * 1024, ge=1)
    zoj_api_url: str = ""
    zoj_public_origin: str = "https://zoj.kr"
    zoj_bridge_secret: SecretStr | None = None
    zoj_ca_file: Path | None = None
    mail_relay_url: str = ""
    mail_relay_secret: SecretStr | None = None
    mail_relay_ca_file: Path | None = None
    auth_mode: str = "zoj"
    auth_api_url: str = ""
    auth_bridge_secret: SecretStr | None = None
    auth_ca_file: Path | None = None
    sso_cookie_domain: str | None = None

    @field_validator("zoj_ca_file", "mail_relay_ca_file", "auth_ca_file", mode="before")
    @classmethod
    def optional_ca(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_production(self):
        if self.execution_transport is None:
            self.execution_transport = "agent" if self.env == "production" else "local"
        if self.env not in {"development", "test", "production"}:
            raise ValueError("Unknown EDITOR_ENV")
        if self.mail_mode not in {"console", "smtp", "zoj"}:
            raise ValueError("EDITOR_MAIL_MODE must be console, smtp or zoj")
        if self.auth_mode not in {"local","zoj"}:
            raise ValueError("EDITOR_AUTH_MODE must be local or zoj")
        if not 1 <= self.judge_concurrency <= 6:
            raise ValueError("EDITOR_JUDGE_CONCURRENCY must be 1 to 6")
        if self.judge_backend == "zoj" and not self.zoj_api_url:
            raise ValueError("ZOJ judging requires EDITOR_ZOJ_API_URL")
        if self.auth_mode == "zoj":
            target = urlsplit(self.auth_api_url)
            if target.scheme not in {"http","https"} or not target.hostname or target.username or target.password or target.query or target.fragment or target.path.rstrip('/') != '/api':
                raise ValueError("Set a fixed EDITOR_AUTH_API_URL ending in /api")
            if not self.auth_bridge_secret or len(self.auth_bridge_secret.get_secret_value()) < 32:
                raise ValueError("Set a separate EDITOR_AUTH_BRIDGE_SECRET of at least 32 characters")
            if self.env == "production" and target.scheme != "https":
                raise ValueError("Production shared authentication requires HTTPS")
        if self.mail_mode == "zoj" and self.auth_mode != "zoj":
            mail_target = urlsplit(self.mail_relay_url)
            if mail_target.scheme not in {"http", "https"} or not mail_target.hostname or mail_target.username or mail_target.password or mail_target.query or mail_target.fragment or mail_target.path.rstrip("/") != "/api":
                raise ValueError("Set a fixed EDITOR_MAIL_RELAY_URL ending in /api")
            if not self.mail_relay_secret or len(self.mail_relay_secret.get_secret_value()) < 32:
                raise ValueError("Set EDITOR_MAIL_RELAY_SECRET of at least 32 characters")
            if self.env == "production" and mail_target.scheme != "https":
                raise ValueError("Production mail transport requires HTTPS")
        if self.zoj_api_url:
            target = urlsplit(self.zoj_api_url)
            if target.scheme not in {"http", "https"} or not target.hostname or target.username or target.password or target.query or target.fragment or target.path.rstrip("/") != "/api":
                raise ValueError("EDITOR_ZOJ_API_URL must be a fixed HTTP(S) URL ending in /api")
            if self.env == "production" and target.scheme != "https":
                raise ValueError("Production ZOJ transport requires HTTPS")
            if not self.zoj_bridge_secret or len(self.zoj_bridge_secret.get_secret_value()) < 32:
                raise ValueError("Set a separate ZOJ bridge secret of at least 32 characters")
        origin = urlsplit(self.zoj_public_origin)
        if origin.scheme not in {"http", "https"} or not origin.hostname or origin.username or origin.password or origin.path not in {"", "/"} or origin.query or origin.fragment:
            raise ValueError("EDITOR_ZOJ_PUBLIC_ORIGIN must be an HTTP(S) origin")
        if self.env == "production":
            if self.execution_transport != "agent":
                raise ValueError("Production Editor executes tools only on authorized judge agents")
            if not self.zoj_api_url:
                raise ValueError("Production agent transport requires EDITOR_ZOJ_API_URL")
            if not self.public_origin.startswith("https://"):
                raise ValueError("Production requires HTTPS")
            if self.auth_mode != "zoj":
                raise ValueError("Production Editor requires shared ZOJ authentication")
            if len(self.session_secret) < 32 or "development-secret" in self.session_secret or "replace-with" in self.session_secret:
                raise ValueError("Set a random production session secret")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
