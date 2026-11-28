"""Runtime configuration. Everything comes from CASEOPS_* environment variables (or a local .env file).

There are deliberately no defaults for secrets: the app refuses to start without them.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="CASEOPS_", extra="ignore")

    env: Literal["development", "test", "production"] = "production"
    database_url: str
    secret_key: str = Field(min_length=32)
    webhook_secret: str = Field(min_length=32)

    access_token_minutes: int = Field(30, ge=5, le=480)
    cookie_secure: bool = True
    # "none" is required when the frontend is served from a different site than the API (then Secure is mandatory).
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cors_origins: list[str] = Field(default_factory=list)

    storage_dir: str = "./data/uploads"
    max_upload_bytes: int = Field(100 * 1024 * 1024, ge=1)
    max_json_bytes: int = Field(1024 * 1024, ge=1024)
    auto_create_tables: bool = False
    ingestion_provider: Literal["mock", "local"] = "local"
    # How ambiguous numeric dates (03/04/2026) are read. Day > 12 always settles it; this decides the rest.
    ingest_date_order: Literal["mdy", "dmy"] = "mdy"
    whisper_model: str = Field("base", pattern=r"^[A-Za-z0-9_.\-/]{2,80}$")
    ocr_lang: str = Field("eng", pattern=r"^[a-zA-Z_]{2,12}(\+[a-zA-Z_]{2,12}){0,3}$")

    # Contradiction proposals from a language model. Off by default: enabling it sends evidence text to that provider.
    contradiction_provider: Literal["none", "anthropic"] = "none"
    anthropic_api_key: SecretStr | None = None
    contradiction_model: str | None = Field(None, pattern=r"^[A-Za-z0-9_.\-]{2,80}$")

    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = Field(20, ge=1)
    rate_limit_api_per_minute: int = Field(600, ge=1)
    trust_proxy_headers: bool = False  # only behind a proxy you control (see core/ratelimit.py)
    stuck_document_minutes: int = Field(15, ge=1)

    login_free_attempts: int = Field(5, ge=1)
    login_base_lock_seconds: int = Field(30, ge=1)
    login_max_lock_seconds: int = Field(900, ge=1)

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("SameSite=None requires CASEOPS_COOKIE_SECURE=true")
        if self.env == "production" and not self.cookie_secure:
            raise ValueError("CASEOPS_COOKIE_SECURE must be true in production")
        if self.contradiction_provider == "anthropic" and not (self.anthropic_api_key and self.contradiction_model):
            raise ValueError("The anthropic contradiction provider needs CASEOPS_ANTHROPIC_API_KEY and "
                             "CASEOPS_CONTRADICTION_MODEL")
        if any(o.strip() == "*" for o in self.cors_origins):
            raise ValueError("Wildcard CORS origins are not allowed (credentials are used)")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
