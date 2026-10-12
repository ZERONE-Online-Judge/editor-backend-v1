from pathlib import Path
from functools import lru_cache
from pydantic import model_validator
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
    worker_concurrency: int = 2
    job_timeout_seconds: int = 1800
    runtime_revision: str = "editor-runtime-0.1.2"
    max_project_bytes: int = 8 * 1024 * 1024 * 1024
    max_uncompressed_archive_bytes: int = 16 * 1024 * 1024 * 1024
    max_upload_bytes: int = 16 * 1024 * 1024
    max_archive_bytes: int = 128 * 1024 * 1024

    @model_validator(mode="after")
    def validate_production(self):
        if self.env not in {"development", "test", "production"}:
            raise ValueError("Unknown EDITOR_ENV")
        if self.mail_mode not in {"console", "smtp"}:
            raise ValueError("EDITOR_MAIL_MODE must be console or smtp")
        if self.env == "production":
            if not self.public_origin.startswith("https://"):
                raise ValueError("Production requires HTTPS")
            if self.mail_mode != "smtp" or not self.smtp_host:
                raise ValueError("Production requires configured SMTP")
            if len(self.session_secret) < 32 or "development-secret" in self.session_secret or "replace-with" in self.session_secret:
                raise ValueError("Set a random production session secret")
            if not self.database_url.startswith("postgresql"):
                raise ValueError("Production requires PostgreSQL")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
