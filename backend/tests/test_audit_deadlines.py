from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError

from app.db import session_factory
from app.models.audit import AuditLog
from app.models.orm import Contradiction, Deadline, Event
from app.services import deadline_calc as dc
from conftest import API, make_case, upload


# ---------- deadline arithmetic (matches the frontend's core.js) ----------
def test_deadline_math():
    t = date(2026, 10, 14)
    assert dc.compute(45, None, t, date(2026, 11, 24)).due == date(2026, 11, 28)
    assert dc.compute(53, None, t, date(2026, 11, 24)).due == date(2026, 12, 6)
    r = dc.compute(None, 6, t, date(2026, 11, 24))
    assert r.due == date(2027, 4, 14) and r.formula == "2026-10-14 + 6 months = 2027-04-14"
    assert dc.add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)
    assert dc.add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)
    assert dc.add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)
    assert dc.add_months(date(2026, 12, 15), 1) == date(2027, 1, 15)


def test_urgency_boundaries_and_invalid_rules():
    assert [dc.urgency(n) for n in (-1, 0, 7, 8, 30, 31)] == ["Overdue", "Urgent", "Urgent", "Active", "Active", "Upcoming"]
    for args in ((None, None), (1, 1)):
        with pytest.raises(ValueError):
            dc.compute(*args, date(2026, 1, 1), date(2026, 1, 1))


def test_today_is_the_courts_calendar_date():
    now = datetime(2026, 11, 25, 3, 0, tzinfo=timezone.utc)  # 22:00 on the 24th in New York
    assert dc.court_today("America/New_York", now) == date(2026, 11, 24)
    assert dc.court_today("Asia/Kathmandu", now) == date(2026, 11, 25)


# ---------- deadline API ----------
def case_with_proof(c):
    cid = make_case(c)
    upload(c, cid)
    return cid, c.get(f"{API}/cases/{cid}/timeline").json()[0]["source"]["line_id"]


def test_deadline_is_computed_and_traceable_to_proof(authed, monkeypatch):
    monkeypatch.setattr(dc, "utcnow", lambda: datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc))
    cid, line = case_with_proof(authed)
    body = {"title": "Omnibus Motion", "citation": "CPL § 255.20(1)", "rule_days": 45, "trigger_label": "Arraignment",
            "trigger_date": "2026-10-14", "trigger_line_id": line}
    r = authed.post(f"{API}/cases/{cid}/deadlines", json=body)
    assert r.status_code == 201
    d = r.json()
    assert (d["due_date"], d["days_left"], d["urgency"], d["rule_verified"]) == ("2026-11-28", 4, "Urgent", False)
    assert d["source"]["line_id"] == line
    assert authed.get(f"{API}/cases/{cid}/summary").json()["next_deadline"]["days_left"] == 4


def test_deadline_validation(authed):
    cid, line = case_with_proof(authed)
    base = {"title": "T", "citation": "C", "trigger_label": "L", "trigger_date": "2026-10-14", "trigger_line_id": line}
    assert authed.post(f"{API}/cases/{cid}/deadlines", json={**base}).status_code == 422  # no rule
    assert authed.post(f"{API}/cases/{cid}/deadlines", json={**base, "rule_days": 5, "rule_months": 1}).status_code == 422
    assert authed.post(f"{API}/cases/{cid}/deadlines", json={**base, "rule_days": 0}).status_code == 422
    assert authed.post(f"{API}/cases/{cid}/deadlines", json={**base, "rule_days": 5, "trigger_date": "2026-02-30"}).status_code == 422
    other = make_case(authed, "D-2")
    foreign = {**base, "rule_days": 5, "trigger_line_id": line}
    assert authed.post(f"{API}/cases/{other}/deadlines", json=foreign).status_code == 422  # proof must be in this case


# ---------- audit chain ----------
def test_audit_chain_verifies_and_detects_tampering(authed):
    cid, _ = case_with_proof(authed)
    v = authed.get(f"{API}/cases/{cid}/audit/verify").json()
    assert v["valid"] is True and v["checked"] >= 3
    with session_factory()() as db:
        db.execute(update(AuditLog).where(AuditLog.chain_key == cid, AuditLog.seq == 2).values(action="document.deleted"))
        db.commit()
    bad = authed.get(f"{API}/cases/{cid}/audit/verify").json()
    assert bad["valid"] is False and bad["first_bad_seq"] == 2


def test_audit_chain_detects_deleted_entries(authed):
    cid, _ = case_with_proof(authed)
    with session_factory()() as db:
        db.execute(delete(AuditLog).where(AuditLog.chain_key == cid, AuditLog.seq == 2))
        db.commit()
    assert authed.get(f"{API}/cases/{cid}/audit/verify").json()["valid"] is False


# ---------- schema invariants: no insight without proof ----------
def test_database_refuses_insights_without_proof_or_with_two_rules(authed):
    cid, line = case_with_proof(authed)
    import uuid
    from datetime import datetime as dt
    bad_rows = [
        Event(case_id=uuid.UUID(cid), title="x", occurred_at=dt.now(timezone.utc), actors=[], line_id=None),
        Event(case_id=uuid.UUID(cid), title="x", occurred_at=dt.now(timezone.utc), actors=[], line_id=uuid.uuid4()),
        Contradiction(case_id=uuid.UUID(cid), title="x", summary="y", line_a_id=uuid.UUID(line), line_b_id=uuid.uuid4()),
        Deadline(case_id=uuid.UUID(cid), title="x", citation="c", rule_days=1, rule_months=1, trigger_label="l",
                 trigger_date=date(2026, 1, 1), trigger_line_id=uuid.UUID(line)),
    ]
    for row in bad_rows:
        with session_factory()() as db:
            db.add(row)
            with pytest.raises(IntegrityError):
                db.commit()


def test_contradictions_expose_both_quotes_with_sources(authed):
    import uuid
    cid, l1 = case_with_proof(authed)
    upload(authed, cid, "b.pdf", b"%PDF-1.4\nother")
    events = authed.get(f"{API}/cases/{cid}/timeline").json()
    l2 = next(e["source"]["line_id"] for e in events if e["source"]["filename"] == "b.pdf")
    with session_factory()() as db:
        db.add(Contradiction(case_id=uuid.UUID(cid), title="Clash", summary="A vs B", line_a_id=uuid.UUID(l1), line_b_id=uuid.UUID(l2)))
        db.commit()
    c = authed.get(f"{API}/cases/{cid}/contradictions").json()[0]
    assert c["claim_a"]["filename"] == "a.pdf" and c["claim_b"]["filename"] == "b.pdf" and c["claim_a"]["quote"]
    assert authed.get(f"{API}/cases/{cid}/summary").json()["conflicts"] == 1
