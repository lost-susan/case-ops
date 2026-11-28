from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from app.core import totp
from app.core.clock import utcnow
from app.db import session_factory
from app.models.orm import DocLine, Document, Event, User
from app.services.reaper import reap
from conftest import API, EMAIL, PASSWORD, login, make_case, make_user, new_client, upload

NEW_PW = "a-brand-new-passphrase"


# ---------- TOTP ----------
def test_totp_matches_rfc6238_vector_and_rejects_replay():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # ASCII "12345678901234567890" (RFC 6238 appendix B)
    assert totp._code(secret, 59 // 30) == "287082"  # the RFC's 94287082, last six digits
    assert totp.verify(secret, "287082", now=59) == 1
    assert totp.verify(secret, "287082", last_step=1, now=59) is None  # same step again: replay
    assert totp.verify(secret, "000000", now=59) is None and totp.verify(secret, "12345", now=59) is None
    assert totp.verify(secret, "287082", now=59 + 300) is None  # far outside the drift window


def test_secret_sealing_roundtrip_and_wrong_key():
    sealed = totp.seal("ABCDEFGH", "k" * 40)
    assert "ABCDEFGH" not in sealed and totp.unseal(sealed, "k" * 40) == "ABCDEFGH"
    assert totp.unseal(sealed, "other-key" * 5) is None


# ---------- MFA flow ----------
def test_mfa_enrol_login_replay_and_disable(app, settings):
    make_user()
    c = new_client(app)
    login(c)
    setup = c.post(f"{API}/auth/mfa/setup").json()
    secret = setup["secret"]
    assert setup["otpauth_uri"].startswith("otpauth://totp/") and secret in setup["otpauth_uri"]
    with session_factory()() as db:
        stored = db.scalar(select(User.mfa_secret))
        assert stored and secret not in stored  # encrypted at rest
    now_step = int(__import__("time").time() // 30)
    assert c.post(f"{API}/auth/mfa/enable", json={"code": "000000"}).status_code == 422
    assert c.post(f"{API}/auth/mfa/enable", json={"code": totp._code(secret, now_step)}).status_code == 204
    assert c.post(f"{API}/auth/mfa/setup").status_code == 409

    fresh = new_client(app)
    body = {"email": EMAIL, "password": PASSWORD}
    r = fresh.post(f"{API}/auth/login", json=body)
    assert r.status_code == 401 and r.json()["detail"] == "mfa_required" and "co_session" not in r.headers.get("set-cookie", "")
    assert fresh.post(f"{API}/auth/login", json={**body, "totp": "123456"}).status_code == 401
    # the enrolment code was already used, so it cannot be replayed to log in
    assert fresh.post(f"{API}/auth/login", json={**body, "totp": totp._code(secret, now_step)}).status_code == 401
    ok = fresh.post(f"{API}/auth/login", json={**body, "totp": totp._code(secret, now_step + 1)})
    assert ok.status_code == 200 and ok.json()["user"]["mfa_enabled"] is True
    assert fresh.post(f"{API}/auth/login", json={**body, "totp": totp._code(secret, now_step + 1)}).status_code == 401

    fresh.headers["X-CSRF-Token"] = ok.json()["csrf_token"]
    assert fresh.post(f"{API}/auth/mfa/disable", json={"password": "wrong-password-1", "code": totp._code(secret, now_step + 1)}).status_code == 403
    nxt = totp._code(secret, now_step + 1)  # same step as last login: refused as a replay even with the right password
    assert fresh.post(f"{API}/auth/mfa/disable", json={"password": PASSWORD, "code": nxt}).status_code == 403


def test_wrong_mfa_codes_trigger_the_lockout(app):
    make_user()
    c = new_client(app)
    login(c)
    secret = c.post(f"{API}/auth/mfa/setup").json()["secret"]
    c.post(f"{API}/auth/mfa/enable", json={"code": totp._code(secret, int(__import__("time").time() // 30))})
    f = new_client(app)
    for _ in range(5):
        f.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD, "totp": "000001"})
    assert f.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD, "totp": "000001"}).status_code == 429


# ---------- password change ----------
def test_change_password_revokes_sessions(app):
    make_user()
    c, other = new_client(app), new_client(app)
    login(c)
    login(other)
    assert c.post(f"{API}/auth/password", json={"current_password": "nope-nope-nope", "new_password": NEW_PW}).status_code == 403
    assert c.post(f"{API}/auth/password", json={"current_password": PASSWORD, "new_password": "short"}).status_code == 422
    assert c.post(f"{API}/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD}).status_code == 422
    assert c.post(f"{API}/auth/password", json={"current_password": PASSWORD, "new_password": NEW_PW}).status_code == 204
    assert other.get(f"{API}/auth/me").status_code == 401 and c.get(f"{API}/auth/me").status_code == 401
    assert new_client(app).post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}).status_code == 401
    login(new_client(app), EMAIL, NEW_PW)


# ---------- admin user management ----------
def test_user_admin_endpoints_are_admin_only_and_effective(app):
    make_user("boss@firm.com", role="admin")
    make_user("para@firm.com", role="paralegal")
    admin, para = new_client(app), new_client(app)
    login(admin, "boss@firm.com")
    login(para, "para@firm.com")
    assert para.get(f"{API}/users").status_code == 403
    assert para.post(f"{API}/users", json={"email": "x@firm.com", "role": "admin", "password": NEW_PW}).status_code == 403
    r = admin.post(f"{API}/users", json={"email": " New@Firm.com ", "role": "attorney", "password": NEW_PW})
    assert r.status_code == 201 and r.json()["email"] == "new@firm.com"
    assert admin.post(f"{API}/users", json={"email": "new@firm.com", "role": "attorney", "password": NEW_PW}).status_code == 409
    assert admin.post(f"{API}/users", json={"email": "z@firm.com", "role": "attorney", "password": "short"}).status_code == 422
    assert admin.post(f"{API}/users", json={"email": "z@firm.com", "role": "root", "password": NEW_PW}).status_code == 422
    assert "password" not in admin.get(f"{API}/users").text.lower()

    uid = next(u["id"] for u in admin.get(f"{API}/users").json() if u["email"] == "para@firm.com")
    assert admin.post(f"{API}/users/{uid}/disable").json()["is_active"] is False
    assert para.get(f"{API}/auth/me").status_code == 401  # signed out immediately
    assert new_client(app).post(f"{API}/auth/login", json={"email": "para@firm.com", "password": PASSWORD}).status_code == 401
    assert admin.post(f"{API}/users/{uid}/enable").json()["is_active"] is True
    assert admin.post(f"{API}/users/{uid}/reset-password", json={"new_password": NEW_PW}).status_code == 204
    login(new_client(app), "para@firm.com", NEW_PW)
    me = next(u["id"] for u in admin.get(f"{API}/users").json() if u["email"] == "boss@firm.com")
    assert admin.post(f"{API}/users/{me}/disable").status_code == 409  # cannot lock yourself out
    assert admin.post(f"{API}/users/00000000-0000-0000-0000-000000000000/disable").status_code == 404


# ---------- case members ----------
def test_case_sharing_rules(app):
    make_user("owner@firm.com")
    make_user("para@firm.com", role="paralegal")
    make_user("gone@firm.com", active=False)
    o, p = new_client(app), new_client(app)
    login(o, "owner@firm.com")
    login(p, "para@firm.com")
    cid = make_case(o)
    assert p.get(f"{API}/cases/{cid}").status_code == 404
    assert o.post(f"{API}/cases/{cid}/members", json={"email": "nobody@firm.com"}).status_code == 404
    assert o.post(f"{API}/cases/{cid}/members", json={"email": "gone@firm.com"}).status_code == 404  # disabled user
    assert o.post(f"{API}/cases/{cid}/members", json={"email": "para@firm.com", "role": "superuser"}).status_code == 422
    assert o.post(f"{API}/cases/{cid}/members", json={"email": "para@firm.com", "role": "editor"}).status_code == 201
    assert o.post(f"{API}/cases/{cid}/members", json={"email": "para@firm.com"}).status_code == 409
    assert p.get(f"{API}/cases/{cid}").status_code == 200 and upload(p, cid).status_code == 202
    assert p.post(f"{API}/cases/{cid}/members", json={"email": "owner@firm.com"}).status_code == 403  # editors cannot share
    members = o.get(f"{API}/cases/{cid}/members").json()
    assert {m["email"]: m["role"] for m in members} == {"owner@firm.com": "owner", "para@firm.com": "editor"}
    owner_id = next(m["user_id"] for m in members if m["role"] == "owner")
    assert o.delete(f"{API}/cases/{cid}/members/{owner_id}").status_code == 409  # last owner
    pid = next(m["user_id"] for m in members if m["role"] == "editor")
    assert o.delete(f"{API}/cases/{cid}/members/{pid}").status_code == 204
    assert p.get(f"{API}/cases/{cid}").status_code == 404


# ---------- rate limiting ----------
def test_rate_limit_blocks_floods_with_retry_after(settings):
    from fastapi.testclient import TestClient
    from app.main import create_app

    app = create_app(settings.model_copy(update={"rate_limit_enabled": True, "rate_limit_login_per_minute": 3}))
    with TestClient(app) as c:
        codes = [c.post(f"{API}/auth/login", json={"email": "x@y.com", "password": "wrong-password-1"}).status_code
                 for _ in range(5)]
        assert codes[:3] == [401, 401, 401] and codes[3:] == [429, 429]
        r = c.post(f"{API}/auth/login", json={"email": "x@y.com", "password": "wrong-password-1"})
        assert int(r.headers["retry-after"]) >= 1 and r.headers["x-content-type-options"] == "nosniff"
        assert c.get("/healthz").status_code == 200  # unrelated paths are not limited


def test_rate_limit_ignores_spoofed_forwarded_for_unless_trusted():
    import asyncio
    from app.core.ratelimit import RateLimitMiddleware

    async def run(trust):
        seen = []

        async def downstream(scope, receive, send):
            seen.append(1)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = RateLimitMiddleware(downstream, rules=[("/api/", 2)], trust_proxy=trust)
        status = []

        async def send(m):
            if m["type"] == "http.response.start":
                status.append(m["status"])

        for i in range(4):  # attacker rotates X-Forwarded-For every request
            scope = {"type": "http", "method": "GET", "path": "/api/x", "client": ("9.9.9.9", 1),
                     "headers": [(b"x-forwarded-for", f"1.1.1.{i}".encode())]}
            await mw(scope, None, send)
        return status

    assert asyncio.run(run(False)) == [200, 200, 429, 429]  # not trusted: keyed on the socket address
    assert asyncio.run(run(True)) == [200, 200, 200, 200]   # trusted proxy mode keys on the header (documented)


# ---------- reaper ----------
def test_reaper_recovers_stuck_documents(authed, settings):
    cid = make_case(authed)
    doc = upload(authed, cid).json()
    old = utcnow() - timedelta(hours=2)
    with session_factory()() as db:  # simulate a crash mid-ingestion: no lines/events were ever committed
        db.execute(delete(Event))
        db.execute(delete(DocLine))
        db.execute(update(Document).values(status="processing", updated_at=old))
        db.commit()
    assert authed.get(f"{API}/cases/{cid}/documents/{doc['id']}").json()["status"] == "processing"
    assert reap(settings, 15) == 1
    assert authed.get(f"{API}/cases/{cid}/documents/{doc['id']}").json()["status"] == "awaiting_extraction"
    assert reap(settings, 15) == 0  # nothing left to do
    actions = [a["action"] for a in authed.get(f"{API}/cases/{cid}/audit").json()]
    assert "document.requeued" in actions and authed.get(f"{API}/cases/{cid}/audit/verify").json()["valid"] is True
