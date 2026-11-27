import jwt
import pytest
from sqlalchemy import func, select

from app.db import session_factory
from app.models.orm import User
from conftest import API, EMAIL, PASSWORD, login, make_case, make_user, new_client

SQLI = [
    "' OR '1'='1", "' OR 1=1--", "admin'--", "'; DROP TABLE users;--", "' UNION SELECT NULL,NULL--",
    "1; WAITFOR DELAY '0:0:5'--", "' OR SLEEP(5)--", "\\' OR 1=1 -- ", "%27%20OR%20%271%27%3D%271", "') OR ('1'='1",
    EMAIL + "'--", EMAIL + "' OR '1'='1",
]


def test_login_sets_httponly_cookie_and_session(client):
    make_user()
    r = login(client)
    cookie = r.headers["set-cookie"].lower()
    assert "co_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    me = client.get(f"{API}/auth/me")
    assert me.status_code == 200 and me.json()["user"]["email"] == EMAIL
    assert "password" not in r.text.lower()


def test_email_is_case_and_space_insensitive(client):
    make_user()
    assert client.post(f"{API}/auth/login", json={"email": "  LAWYER@Organization.com ", "password": PASSWORD}).status_code == 200


def test_wrong_password_and_unknown_email_are_indistinguishable(client):
    make_user()
    a = client.post(f"{API}/auth/login", json={"email": EMAIL, "password": "wrong-password-1"})
    b = client.post(f"{API}/auth/login", json={"email": "nobody@organization.com", "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_disabled_account_cannot_login(client):
    make_user(active=False)
    assert client.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 401


@pytest.mark.parametrize("payload", SQLI)
def test_sql_injection_never_authenticates(client, payload):
    make_user()
    for body in ({"email": payload, "password": PASSWORD}, {"email": EMAIL, "password": payload},
                 {"email": payload, "password": payload}):
        r = client.post(f"{API}/auth/login", json=body)
        assert r.status_code in (401, 422), (body, r.status_code)  # never 200, never a 500
        assert "co_session" not in r.headers.get("set-cookie", "")
    with session_factory()() as db:  # the DROP TABLE payload changed nothing
        assert db.scalar(select(func.count(User.id))) == 1


def test_malformed_login_bodies_are_rejected_cleanly(client):
    for body in ({}, {"email": EMAIL}, {"email": EMAIL, "password": ""}, {"email": EMAIL, "password": "x" * 129},
                 {"email": EMAIL, "password": "a\x00b"}, {"email": ["x"], "password": "y"}, {"email": EMAIL, "password": 5},
                 {"email": EMAIL, "password": PASSWORD, "role": "admin"}):
        assert client.post(f"{API}/auth/login", json=body).status_code == 422


def test_lockout_applies_to_unknown_emails_too_and_blocks_correct_password(client):
    make_user()
    for _ in range(5):
        client.post(f"{API}/auth/login", json={"email": EMAIL, "password": "wrong-password-1"})
    r = client.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    for _ in range(5):
        client.post(f"{API}/auth/login", json={"email": "ghost@organization.com", "password": "wrong-password-1"})
    assert client.post(f"{API}/auth/login", json={"email": "ghost@organization.com", "password": "x"}).status_code == 429


def test_logout_revokes_the_session_even_if_the_cookie_is_replayed(client, app):
    make_user()
    login(client)
    token = client.cookies.get("co_session")
    assert client.post(f"{API}/auth/logout").status_code == 204
    replay = new_client(app)
    replay.cookies.set("co_session", token)
    assert replay.get(f"{API}/auth/me").status_code == 401


def test_csrf_header_is_required_for_state_changes(client):
    make_user()
    login(client)
    good = client.headers.pop("X-CSRF-Token")
    body = {"title": "T", "docket": "D-1"}
    assert client.post(f"{API}/cases", json=body).status_code == 403
    assert client.post(f"{API}/cases", json=body, headers={"X-CSRF-Token": "0" * 64}).status_code == 403
    assert client.post(f"{API}/cases", json=body, headers={"X-CSRF-Token": good}).status_code == 201


def test_unauthenticated_requests_are_rejected(client):
    for path in ("/auth/me", "/cases"):
        assert client.get(API + path).status_code == 401


def test_forged_and_unsigned_tokens_are_rejected(client, settings, app):
    make_user()
    login(client)
    claims = jwt.decode(client.cookies.get("co_session"), settings.secret_key, algorithms=["HS256"])
    for token in (jwt.encode(claims, "wrong-secret-" * 4, algorithm="HS256"), jwt.encode(claims, None, algorithm="none"),
                  "garbage", ""):
        c = new_client(app)
        c.cookies.set("co_session", token)
        assert c.get(f"{API}/auth/me").status_code == 401
    expired = dict(claims, exp=claims["iat"] - 10)
    c = new_client(app)
    c.cookies.set("co_session", jwt.encode(expired, settings.secret_key, algorithm="HS256"))
    assert c.get(f"{API}/auth/me").status_code == 401


def test_security_headers_and_cors(client):
    r = client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    ok = client.get("/healthz", headers={"Origin": "http://localhost:8080"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:8080"
    assert ok.headers["access-control-allow-credentials"] == "true"
    bad = client.get("/healthz", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers
