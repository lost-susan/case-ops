from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401  (register tables)
from app.api.v1 import auth, cases, documents, users, webhooks
from app.config import Settings, get_settings
from app.core.middleware import BodySizeLimitMiddleware, SecurityHeadersMiddleware
from app.core.ratelimit import RateLimitMiddleware
from app.db import Base, get_engine, init_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    init_engine(settings.database_url)
    if settings.auto_create_tables:  # dev/test convenience; production schema is managed by migrations
        Base.metadata.create_all(get_engine())

    production = settings.env == "production"
    app = FastAPI(
        title="CASE-OPS API", version="1.0.0",
        docs_url=None if production else "/docs", redoc_url=None, openapi_url=None if production else "/openapi.json",
    )
    app.dependency_overrides[get_settings] = lambda: settings

    # Added innermost-first: CORS ends up outermost so even 413/401 answers carry the right headers.
    app.add_middleware(BodySizeLimitMiddleware, json_limit=settings.max_json_bytes, upload_limit=settings.max_upload_bytes)
    if settings.rate_limit_enabled:
        app.add_middleware(RateLimitMiddleware, trust_proxy=settings.trust_proxy_headers, rules=[
            ("/api/v1/auth/login", settings.rate_limit_login_per_minute),
            ("/api/v1/webhooks", settings.rate_limit_api_per_minute),
            ("/api/", settings.rate_limit_api_per_minute)])
    app.add_middleware(SecurityHeadersMiddleware, production=production)  # outside the limiter: 429s carry headers too
    app.add_middleware(
        CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "X-CSRF-Token"], max_age=600,
    )

    for module in (auth, users, cases, documents, webhooks):
        app.include_router(module.router, prefix="/api/v1")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
