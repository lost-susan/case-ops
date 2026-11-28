import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.core.security import hash_password
from app.db import Base, get_engine, session_factory
from app.main import create_app
from app.models.orm import User

EMAIL = "lawyer@organization.com"
PASSWORD = "demo-access-key-123"
PDF = b"%PDF-1.4\n%caseops test file\n"
API = "/api/v1"


@pytest.fixture
def settings(tmp_path):
    # CASEOPS_TEST_DATABASE_URL points the suite at Postgres (CI); default is a throw-away SQLite file.
    return Settings(
        env="test", database_url=os.environ.get("CASEOPS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 't.db'}", secret_key="s" * 48, webhook_secret="w" * 48,
        cookie_secure=False, storage_dir=str(tmp_path / "uploads"), auto_create_tables=True,
        cors_origins=["http://localhost:8080"], max_upload_bytes=1024 * 1024, ingestion_provider="mock", rate_limit_enabled=False,
    )


@pytest.fixture
def app(settings):
    application = create_app(settings)
    yield application
    Base.metadata.drop_all(get_engine())  # clean slate for the next test (matters on a shared Postgres)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def new_client(app):
    return TestClient(app)


def make_user(email=EMAIL, role="attorney", password=PASSWORD, active=True):
    with session_factory()() as db:
        user = User(email=email, password_hash=hash_password(password), role=role, is_active=active)
        db.add(user)
        db.commit()
        return user.id


def login(c, email=EMAIL, password=PASSWORD):
    r = c.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return r


def make_case(c, docket="NY-2026-CR", title="State vs. Alex Miller"):
    r = c.post(f"{API}/cases", json={"title": title, "docket": docket})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def upload(c, case_id, name="a.pdf", data=PDF, ctype="application/pdf"):
    return c.post(f"{API}/cases/{case_id}/documents", files={"file": (name, data, ctype)})


@pytest.fixture
def authed(client):
    make_user()
    login(client)
    return client
