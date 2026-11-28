"""Runs ingestion for an uploaded document. Milestone 1 uses FastAPI background tasks in-process; the function
is self-contained (own DB session, atomic claim) so it can move to a real queue worker unchanged."""
import logging
import uuid
from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy import update

from ai_engine.pipelines.ingestion import ExtractionResult, get_provider
from app.core.clock import utcnow
from app.db import session_factory
from app.models.orm import Case, DocLine, Document, Event
from app.services import audit_logger
from app.services.storage import LocalStorage

log = logging.getLogger("caseops.ingestion")
MAX_LINES = 50_000
ALLOWED_FINAL = {"awaiting_extraction", "parsed", "transcribed"}


def enqueue_ingestion(background: BackgroundTasks, document_id: uuid.UUID, storage_dir: str, provider: str,
                      options: dict | None = None) -> None:
    background.add_task(run_ingestion, document_id, storage_dir, provider, options)


def _validate(result: ExtractionResult) -> None:
    if result.status not in ALLOWED_FINAL:
        raise ValueError(f"Provider returned invalid status {result.status!r}")
    if not 0 < len(result.lines) <= MAX_LINES:
        raise ValueError("Provider returned no lines or too many lines")
    if result.confidence is not None and not 0 <= result.confidence <= 100:
        raise ValueError("Confidence out of range")
    for ev in result.events:
        if not 0 <= ev.line_index < len(result.lines):
            raise ValueError("Event without valid source proof line")  # PRD invariant
        if ev.occurred_at.tzinfo is None:
            raise ValueError("Event time must be timezone-aware")


def run_ingestion(document_id: uuid.UUID, storage_dir: str, provider_name: str, options: dict | None = None) -> None:
    with session_factory()() as db:
        # Atomic claim: only one worker can move queued -> processing, so a retry never double-processes.
        claimed = db.execute(
            update(Document).where(Document.id == document_id, Document.status == "queued").values(status="processing")
        ).rowcount
        db.commit()
        if claimed != 1:
            return
        doc = db.get(Document, document_id)
        try:
            result = get_provider(provider_name, **(options or {})).process(
                path=LocalStorage(storage_dir).path(doc.storage_key), kind=doc.kind, filename=doc.filename,
                sha256=doc.sha256, size_bytes=doc.size_bytes, received_at=utcnow(),
                case_timezone=db.get(Case, doc.case_id).timezone)
            _validate(result)
            rows = [
                DocLine(document_id=doc.id, page=ln.page, idx=i, text=ln.text[:20000],
                        bbox=list(ln.bbox) if ln.bbox else None, start_sec=ln.start_sec, confidence=ln.confidence)
                for i, ln in enumerate(result.lines)
            ]
            db.add_all(rows)
            db.flush()
            for ev in result.events:
                db.add(Event(case_id=doc.case_id, title=ev.title[:300], occurred_at=ev.occurred_at,
                             actors=list(ev.actors), line_id=rows[ev.line_index].id, status=ev.status))
            doc.status, doc.ocr_confidence = result.status, result.confidence
            audit_logger.append(db, action="document.ingested", case_id=doc.case_id, source_hash=doc.sha256,
                                detail={"document_id": str(doc.id), "lines": len(rows), "events": len(result.events),
                                        "status": result.status})
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Ingestion failed for document %s", document_id)
            doc = db.get(Document, document_id)
            doc.status = "failed"
            audit_logger.append(db, action="document.ingestion_failed", case_id=doc.case_id, source_hash=doc.sha256,
                                detail={"document_id": str(doc.id)})
            db.commit()
