from sqlalchemy import func, select

from app.db import session_factory
from app.models.orm import Case, CaseMember
from conftest import API, PDF, login, make_case, make_user, new_client, upload


def two_firms(app):
    a, b = new_client(app), new_client(app)
    make_user("a@firm-a.com")
    make_user("b@firm-b.com")
    login(a, "a@firm-a.com")
    login(b, "b@firm-b.com")
    return a, b


def test_other_users_cannot_see_or_touch_a_case(app):
    a, b = two_firms(app)
    cid = make_case(a)
    doc = upload(a, cid)
    assert doc.status_code == 202
    did = doc.json()["id"]
    line_id = a.get(f"{API}/cases/{cid}/timeline").json()[0]["source"]["line_id"]
    probes = [f"/cases/{cid}", f"/cases/{cid}/timeline", f"/cases/{cid}/contradictions", f"/cases/{cid}/deadlines",
              f"/cases/{cid}/summary", f"/cases/{cid}/documents", f"/cases/{cid}/documents/{did}",
              f"/cases/{cid}/documents/{did}/download", f"/cases/{cid}/proofs/{line_id}", f"/cases/{cid}/audit",
              f"/cases/{cid}/audit/verify"]
    for p in probes:
        assert a.get(API + p).status_code == 200, p
        assert b.get(API + p).status_code == 404, p  # 404, not 403: don't confirm the case exists
    assert upload(b, cid).status_code == 404
    assert b.get(f"{API}/cases").json() == []


def test_proof_lookup_cannot_cross_cases(app):
    a, _ = two_firms(app)
    c1, c2 = make_case(a, "D-1"), make_case(a, "D-2")
    upload(a, c1)
    line = a.get(f"{API}/cases/{c1}/timeline").json()[0]["source"]["line_id"]
    assert a.get(f"{API}/cases/{c2}/proofs/{line}").status_code == 404


def test_only_attorneys_create_cases_and_viewers_cannot_upload(app):
    a = new_client(app)
    make_user("a@firm-a.com")
    login(a, "a@firm-a.com")
    cid = make_case(a)
    p = new_client(app)
    pid = make_user("para@firm-a.com", role="paralegal")
    login(p, "para@firm-a.com")
    assert p.post(f"{API}/cases", json={"title": "x", "docket": "D-9"}).status_code == 403
    with session_factory()() as db:
        db.add(CaseMember(case_id=__import__("uuid").UUID(cid), user_id=pid, role="viewer"))
        db.commit()
    assert p.get(f"{API}/cases/{cid}").status_code == 200
    assert upload(p, cid).status_code == 403


def test_case_input_validation_and_sql_text_is_stored_inertly(client):
    make_user("a@firm-a.com")
    login(client, "a@firm-a.com")
    assert client.post(f"{API}/cases", json={"title": "t", "docket": "x'; DROP TABLE cases;--"}).status_code == 422
    assert client.post(f"{API}/cases", json={"title": "t", "docket": "D-1", "timezone": "Mars/Base"}).status_code == 422
    assert client.post(f"{API}/cases", json={"title": "t", "docket": "D-1", "role": "owner"}).status_code == 422
    assert client.post(f"{API}/cases", json={"title": "", "docket": "D-1"}).status_code == 422
    nasty = "'; DROP TABLE cases;-- <script>alert(1)</script>"
    cid = make_case(client, "D-2", nasty)
    assert client.get(f"{API}/cases/{cid}").json()["title"] == nasty  # stored and returned verbatim (JSON, no HTML)
    with session_factory()() as db:
        assert db.scalar(select(func.count(Case.id))) == 1
    assert client.post(f"{API}/cases", json={"title": "dup", "docket": "D-2"}).status_code == 409
    assert client.get(f"{API}/cases/not-a-uuid").status_code == 422
