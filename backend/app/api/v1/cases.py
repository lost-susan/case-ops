import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.api.deps import CaseContext, Principal, case_access, current_principal
from app.db import get_db
from app.models.audit import AuditLog
from app.models.orm import Case, CaseMember, Contradiction, Deadline, DocLine, Document, Event
from app.models.schemas import (AuditOut, CaseCreate, CaseOut, ContradictionOut, DeadlineCreate, DeadlineOut,
                                DocumentOut, EventOut, ProofLineOut, ProofOut, SummaryOut)
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


@router.get("/{case_id}/contradictions", response_model=list[ContradictionOut])
def contradictions(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    la, lb, da, dbb = aliased(DocLine), aliased(DocLine), aliased(Document), aliased(Document)
    rows = db.execute(
        select(Contradiction, la, da, lb, dbb)
        .join(la, Contradiction.line_a_id == la.id).join(da, la.document_id == da.id)
        .join(lb, Contradiction.line_b_id == lb.id).join(dbb, lb.document_id == dbb.id)
        .where(Contradiction.case_id == ctx.case.id).order_by(Contradiction.title)
    ).all()
    return [ContradictionOut(id=c.id, title=c.title, summary=c.summary, claim_a=source_ref(a_doc, a),
                             claim_b=source_ref(b_doc, b)) for c, a, a_doc, b, b_doc in rows]


@router.get("/{case_id}/summary", response_model=SummaryOut)
def summary(ctx: CaseContext = Depends(case_access("viewer")), db: Session = Depends(get_db)):
    files, mean = db.execute(
        select(func.count(Document.id), func.avg(Document.ocr_confidence)).where(Document.case_id == ctx.case.id)
    ).one()
    conflicts = db.scalar(select(func.count(Contradiction.id)).where(Contradiction.case_id == ctx.case.id))
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
