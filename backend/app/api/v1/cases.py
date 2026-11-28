import uuid
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.api.deps import CaseContext, Principal, case_access, current_principal
from app.config import Settings, get_settings
from app.db import get_db
from app.models.audit import AuditLog
from app.models.orm import Case, CaseMember, Contradiction, Deadline, DocLine, Document, Event, User
from app.models.schemas import (AuditOut, CaseCreate, CaseOut, ContradictionCreate, ContradictionOut, DeadlineCreate,
                                DeadlineOut, DocumentOut, EventOut, MemberAdd, MemberOut, ProofLineOut, ProofOut,
                                ReviewIn, SearchHitOut, SearchOut, SummaryOut)
from app.services.contradiction_scan import pair_key, run_scan
from app.services import audit_logger
from app.services.deadline_calc import compute, court_today
from app.services.proofs import source_ref

router = APIRouter(prefix="/cases", tags=["cases"])
WINDOW = 50  # lines shown either side of a cited line in long text/audio sources


@router.get("", response_model=list[CaseOut])
def list_cases(db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    return db.scalars(
        select(Case).join(CaseMember, CaseMember.case_id == Case.id)
        .where(CaseMember.user_id == principal.user.id).order_by(Case.created_at.desc())
    ).all()


@router.post("", response_model=CaseOut, status_code=201)
def create_case(body: CaseCreate, db: Session = Depends(get_db), principal: Principal = Depends(current_principal)):
    if principal.user.role not in ("admin", "attorney"):
        raise HTTPException(403, "Only attorneys can create cases")
    case = Case(title=body.title, docket=body.docket, timezone=body.timezone, created_by=principal.user.id)
    db.add(case)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A case with this docket already exists")
    db.add(CaseMember(case_id=case.id, user_id=principal.user.id, role="owner"))
    audit_logger.append(db, action="case.created", case_id=case.id, user_id=principal.user.id,
                        detail={"docket": case.docket})
    db.commit()
    return case


@router.get("/{case_id}", response_model=CaseOut)
def get_case(ctx: CaseContext = Depends(case_access("viewer"))):
    return ctx.case


def _deadline_out(d: Deadline, doc: Document, line: DocLine, today) -> DeadlineOut:
    r = compute(d.rule_days, d.rule_months, d.trigger_date, today)
    return DeadlineOut(
        id=d.id, title=d.title, citation=d.citation, trigger_label=d.trigger_label, trigger_date=d.trigger_date,
        due_date=r.due, days_left=r.days_left, urgency=r.urgency, formula=r.formula, rule_verified=d.rule_verified,
        source=source_ref(doc, line))


def _deadlines(db: Session, case) -> list[DeadlineOut]:
    today = court_today(case.timezone)
    rows = db.execute(
        select(Deadline, Document, DocLine).join(DocLine, Deadline.trigger_line_id == DocLine.id)
        .join(Document, DocLine.document_id == Document.id).where(Deadline.case_id == case.id)
    ).all()
    return sorted((_deadline_out(d, doc, ln, today) for d, doc, ln in rows), key=lambda o: o.days_left)


@router.get("/{case_id}/deadlines", response_model=list[DeadlineOut])
def list_deadlines(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    return _deadlines(db, ctx.case)


@router.post("/{case_id}/deadlines", response_model=DeadlineOut, status_code=201)
def create_deadline(body: DeadlineCreate, ctx: CaseContext = Depends(case_access("editor")),
                    db: Session = Depends(get_db)):
    found = db.execute(
        select(DocLine, Document).join(Document, DocLine.document_id == Document.id)
        .where(DocLine.id == body.trigger_line_id, Document.case_id == ctx.case.id)
    ).first()
    if found is None:
        raise HTTPException(422, "trigger_line_id must reference a line in this case's documents")  # proof required
    d = Deadline(case_id=ctx.case.id, title=body.title, citation=body.citation, rule_days=body.rule_days,
                 rule_months=body.rule_months, trigger_label=body.trigger_label, trigger_date=body.trigger_date,
                 trigger_line_id=body.trigger_line_id, rule_verified=False)
    db.add(d)
    db.flush()
    audit_logger.append(db, action="deadline.created", case_id=ctx.case.id, user_id=ctx.user.id,
                        detail={"deadline_id": str(d.id), "citation": d.citation})
    db.commit()
    return _deadline_out(d, found.Document, found.DocLine, court_today(ctx.case.timezone))


@router.get("/{case_id}/timeline", response_model=list[EventOut])
def timeline(status: Literal["verified", "conflict", "pending"] | None = None, limit: int = Query(500, ge=1, le=1000),
             ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    q = (select(Event, DocLine, Document).join(DocLine, Event.line_id == DocLine.id)
         .join(Document, DocLine.document_id == Document.id).where(Event.case_id == ctx.case.id))
    if status:
        q = q.where(Event.status == status)
    rows = db.execute(q.order_by(Event.occurred_at, Event.id).limit(limit)).all()
    return [EventOut(id=e.id, title=e.title, occurred_at=e.occurred_at, actors=e.actors, status=e.status,
                     source=source_ref(doc, ln)) for e, ln, doc in rows]


def _contradiction_rows(db: Session, case_id, only_id=None):
    la, lb, da, dbb = aliased(DocLine), aliased(DocLine), aliased(Document), aliased(Document)
    q = (select(Contradiction, la, da, lb, dbb)
         .join(la, Contradiction.line_a_id == la.id).join(da, la.document_id == da.id)
         .join(lb, Contradiction.line_b_id == lb.id).join(dbb, lb.document_id == dbb.id)
         .where(Contradiction.case_id == case_id))
    q = q.where(Contradiction.id == only_id) if only_id else q.where(Contradiction.review_state != "dismissed")
    return db.execute(q.order_by(Contradiction.title)).all()


def _contradiction_out(c, a, a_doc, b, b_doc) -> ContradictionOut:
    return ContradictionOut(id=c.id, title=c.title, summary=c.summary, claim_a=source_ref(a_doc, a),
                            claim_b=source_ref(b_doc, b), origin=c.origin, review_state=c.review_state)


@router.get("/{case_id}/contradictions", response_model=list[ContradictionOut])
def contradictions(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    return [_contradiction_out(*r) for r in _contradiction_rows(db, ctx.case.id)]


def _lines_in_case(db: Session, case_id, ids) -> int:
    return db.scalar(select(func.count(DocLine.id)).join(Document, DocLine.document_id == Document.id)
                     .where(DocLine.id.in_(ids), Document.case_id == case_id))


@router.post("/{case_id}/contradictions", response_model=ContradictionOut, status_code=201)
def create_contradiction(body: ContradictionCreate, ctx: CaseContext = Depends(case_access("editor")),
                         db: Session = Depends(get_db)):
    """A lawyer-entered contradiction. Both sides must be real lines of this case's documents (proof required)."""
    if _lines_in_case(db, ctx.case.id, {body.line_a_id, body.line_b_id}) != 2:
        raise HTTPException(422, "Both lines must belong to documents in this case")
    a, b = pair_key(body.line_a_id, body.line_b_id)
    c = Contradiction(case_id=ctx.case.id, title=body.title, summary=body.summary, line_a_id=a, line_b_id=b,
                      origin="manual", review_state="confirmed")
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "These two lines are already recorded as a contradiction")
    db.execute(update(Event).where(Event.case_id == ctx.case.id, Event.status == "pending",
                                   Event.line_id.in_([a, b])).values(status="conflict"))
    audit_logger.append(db, action="contradiction.created", case_id=ctx.case.id, user_id=ctx.user.id,
                        detail={"contradiction_id": str(c.id)})
    db.commit()
    return _contradiction_out(*_contradiction_rows(db, ctx.case.id, c.id)[0])


@router.post("/{case_id}/contradictions/scan", status_code=202)
def scan_contradictions(background: BackgroundTasks, ctx: CaseContext = Depends(case_access("editor")),
                        settings: Settings = Depends(get_settings)):
    """Ask the configured model for contradiction proposals. Results are unreviewed until a lawyer confirms them."""
    if settings.contradiction_provider == "none":
        raise HTTPException(409, "No contradiction provider is configured")
    background.add_task(run_scan, ctx.case.id, ctx.user.id, settings)
    return {"status": "queued"}


@router.post("/{case_id}/contradictions/{contradiction_id}/review", response_model=ContradictionOut)
def review_contradiction(contradiction_id: uuid.UUID, body: ReviewIn, ctx: CaseContext = Depends(case_access("editor")),
                         db: Session = Depends(get_db)):
    rows = _contradiction_rows(db, ctx.case.id, contradiction_id)
    if not rows:
        raise HTTPException(404, "Contradiction not found")
    c = rows[0][0]
    c.review_state = "confirmed" if body.decision == "confirm" else "dismissed"
    audit_logger.append(db, action=f"contradiction.{body.decision}ed", case_id=ctx.case.id, user_id=ctx.user.id,
                        detail={"contradiction_id": str(c.id), "origin": c.origin})
    db.commit()
    return _contradiction_out(*rows[0])


_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


@router.get("/{case_id}/search", response_model=SearchOut)
def search(q: str = Query(..., min_length=2, max_length=200), limit: int = Query(25, ge=1, le=100),
           ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    """Grounded search: only text that really exists in this case's documents, each hit pointing at its source line.
    Every word must appear (case-insensitive); LIKE wildcards in the query are escaped."""
    terms = [t for t in q.split() if len(t) >= 2][:8]
    if not terms or any(len(t) > 64 for t in terms):
        raise HTTPException(422, "Use 1-8 words of 2-64 characters")
    cond = [DocLine.text.ilike(f"%{t.translate(_LIKE_ESCAPE)}%", escape="\\") for t in terms]
    base = (select(DocLine, Document).join(Document, DocLine.document_id == Document.id)
            .where(Document.case_id == ctx.case.id, Document.status != "failed", *cond))
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    rows = db.execute(base.order_by(Document.created_at, DocLine.page, DocLine.idx).limit(limit)).all()
    hits = []
    for ln, doc in rows:
        low, first = ln.text.casefold(), terms[0].casefold()
        at = max(0, low.find(first) - 80)
        snippet = ("…" if at else "") + ln.text[at:at + 240] + ("…" if at + 240 < len(ln.text) else "")
        hits.append(SearchHitOut(source=source_ref(doc, ln), snippet=snippet))
    return SearchOut(query=q, total=total, hits=hits)


@router.get("/{case_id}/members", response_model=list[MemberOut])
def list_members(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    rows = db.execute(select(CaseMember.user_id, User.email, CaseMember.role)
                      .join(User, User.id == CaseMember.user_id).where(CaseMember.case_id == ctx.case.id)
                      .order_by(User.email)).all()
    return [MemberOut(user_id=u, email=e, role=r) for u, e, r in rows]


@router.post("/{case_id}/members", response_model=MemberOut, status_code=201)
def add_member(body: MemberAdd, ctx: CaseContext = Depends(case_access("owner")), db: Session = Depends(get_db)):
    """Share a case with an existing active user of this deployment (single-organisation model)."""
    user = db.scalar(select(User).where(User.email == body.email, User.is_active.is_(True)))
    if user is None:
        raise HTTPException(404, "No active user with that email")
    if db.get(CaseMember, (ctx.case.id, user.id)) is not None:
        raise HTTPException(409, "Already a member of this case")
    db.add(CaseMember(case_id=ctx.case.id, user_id=user.id, role=body.role))
    audit_logger.append(db, action="case.member_added", case_id=ctx.case.id, user_id=ctx.user.id,
                        detail={"target": str(user.id), "role": body.role})
    db.commit()
    return MemberOut(user_id=user.id, email=user.email, role=body.role)


@router.delete("/{case_id}/members/{user_id}", status_code=204)
def remove_member(user_id: uuid.UUID, ctx: CaseContext = Depends(case_access("owner")), db: Session = Depends(get_db)):
    member = db.get(CaseMember, (ctx.case.id, user_id))
    if member is None:
        raise HTTPException(404, "Member not found")
    if member.role == "owner" and db.scalar(
            select(func.count()).select_from(CaseMember).where(CaseMember.case_id == ctx.case.id,
                                                               CaseMember.role == "owner")) <= 1:
        raise HTTPException(409, "A case must keep at least one owner")
    db.delete(member)
    audit_logger.append(db, action="case.member_removed", case_id=ctx.case.id, user_id=ctx.user.id,
                        detail={"target": str(user_id)})
    db.commit()


@router.get("/{case_id}/summary", response_model=SummaryOut)
def summary(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    files, mean = db.execute(
        select(func.count(Document.id), func.avg(Document.ocr_confidence)).where(Document.case_id == ctx.case.id)
    ).one()
    conflicts = db.scalar(select(func.count(Contradiction.id)).where(
        Contradiction.case_id == ctx.case.id, Contradiction.review_state != "dismissed"))
    upcoming = [d for d in _deadlines(db, ctx.case) if d.days_left >= 0]
    return SummaryOut(indexed_files=files, mean_confidence=round(mean, 1) if mean is not None else None,
                      conflicts=conflicts, next_deadline=upcoming[0] if upcoming else None)


@router.get("/{case_id}/documents", response_model=list[DocumentOut])
def list_documents(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    return db.scalars(select(Document).where(Document.case_id == ctx.case.id).order_by(Document.created_at.desc())).all()


@router.get("/{case_id}/proofs/{line_id}", response_model=ProofOut)
def proof(line_id: uuid.UUID, ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    """The raw source behind any insight: the cited line plus its surrounding text, ready to highlight."""
    row = db.execute(
        select(DocLine, Document).join(Document, DocLine.document_id == Document.id)
        .where(DocLine.id == line_id, Document.case_id == ctx.case.id)
    ).first()
    if row is None:
        raise HTTPException(404, "Proof not found")
    line, doc = row
    q = select(DocLine).where(DocLine.document_id == doc.id)
    if doc.kind in ("pdf", "jpg", "png"):
        q = q.where(DocLine.page == line.page)
    else:
        q = q.where(DocLine.idx.between(line.idx - WINDOW, line.idx + WINDOW))
    lines = db.scalars(q.order_by(DocLine.page, DocLine.idx)).all()
    return ProofOut(
        document=DocumentOut.model_validate(doc), page=line.page, highlight_index=line.idx, confidence=line.confidence,
        lines=[ProofLineOut(idx=x.idx, text=x.text, bbox=x.bbox, start_sec=x.start_sec) for x in lines])


@router.get("/{case_id}/audit", response_model=list[AuditOut])
def audit(limit: int = Query(50, ge=1, le=200), before_seq: int | None = Query(None, ge=1),
          ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    q = select(AuditLog).where(AuditLog.chain_key == str(ctx.case.id))
    if before_seq:
        q = q.where(AuditLog.seq < before_seq)
    return db.scalars(q.order_by(AuditLog.seq.desc()).limit(limit)).all()


@router.get("/{case_id}/audit/verify")
def verify_audit(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    return audit_logger.verify_chain(db, str(ctx.case.id))
