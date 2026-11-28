"""Asks the configured provider for contradiction proposals over a case's dated lines, validates them against the raw
text and stores survivors as unreviewed AI proposals. Safe to run twice: pairs are unique per case."""
import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ai_engine.pipelines.contradictions import (MAX_CANDIDATES, Candidate, ProviderError, get_contradiction_provider,
                                                validate)
from app.config import Settings
from app.db import session_factory
from app.models.orm import Contradiction, DocLine, Document, Event
from app.services import audit_logger
from app.services.proofs import locator

log = logging.getLogger("caseops.contradictions")


def provider_from_settings(settings: Settings):
    if settings.contradiction_provider == "anthropic":
        return get_contradiction_provider("anthropic", api_key=settings.anthropic_api_key.get_secret_value(),
                                          model=settings.contradiction_model)
    return get_contradiction_provider("none")


def pair_key(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) <= str(b) else (b, a)  # one stored order per pair so the unique key catches reversals


def run_scan(case_id: uuid.UUID, user_id: uuid.UUID, settings: Settings) -> None:
    with session_factory()() as db:
        try:
            rows = db.execute(
                select(Event, DocLine, Document).join(DocLine, Event.line_id == DocLine.id)
                .join(Document, DocLine.document_id == Document.id).where(Event.case_id == case_id)
                .order_by(Event.occurred_at, Event.id).limit(MAX_CANDIDATES)
            ).all()
            lines = {}
            cands: list[Candidate] = []
            for ev, ln, doc in rows:
                if ln.id in lines:
                    continue
                lines[ln.id] = len(cands)
                cands.append(Candidate(index=len(cands), filename=doc.filename, locator=locator(doc, ln), text=ln.text,
                                       speaker=ev.actors[0] if ev.actors else None, occurred_at=ev.occurred_at))
            by_index = {i: lid for lid, i in lines.items()}
            proposals = provider_from_settings(settings).find(cands)  # network call; no DB locks held meanwhile
            accepted, rejected = validate(proposals, cands)
            created = 0
            for p in accepted:
                a, b = pair_key(by_index[p.a], by_index[p.b])
                try:
                    with db.begin_nested():
                        db.add(Contradiction(case_id=case_id, title=p.title[:300], summary=p.summary, line_a_id=a,
                                             line_b_id=b, origin="ai", review_state="unreviewed"))
                        db.flush()
                    created += 1
                except IntegrityError:
                    continue  # already known
                db.execute(update(Event).where(Event.case_id == case_id, Event.status == "pending",
                                               Event.line_id.in_([a, b])).values(status="conflict"))
            audit_logger.append(db, action="contradiction.scan", case_id=case_id, user_id=user_id,
                                detail={"provider": settings.contradiction_provider, "lines": len(cands),
                                        "proposed": len(proposals), "created": created, "rejected": rejected})
            db.commit()
        except (ProviderError, Exception) as exc:  # a failed scan must never take the API down
            db.rollback()
            log.exception("Contradiction scan failed for case %s", case_id)
            audit_logger.append(db, action="contradiction.scan_failed", case_id=case_id, user_id=user_id,
                                detail={"error": type(exc).__name__})
            db.commit()
