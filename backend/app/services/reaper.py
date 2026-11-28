"""Recovers documents whose ingestion never finished (process crashed, container restarted mid-job).

Ingestion runs as an in-process background task, so a crash leaves a document in `queued` or `processing` forever.
`reap` re-queues anything untouched for `older_than_minutes` and runs it again. Run it from cron / a scheduler, e.g.
`python -m app.cli reap`. It is idempotent: run_ingestion claims each document atomically."""
import logging
from datetime import timedelta

from sqlalchemy import select, update

from app.config import Settings
from app.core.clock import utcnow
from app.db import session_factory
from app.models.orm import Document
from app.services import audit_logger
from app.services.task_router import run_ingestion

log = logging.getLogger("caseops.reaper")


def reap(settings: Settings, older_than_minutes: int | None = None) -> int:
    cutoff = utcnow() - timedelta(minutes=older_than_minutes or settings.stuck_document_minutes)
    with session_factory()() as db:
        ids = list(db.scalars(select(Document.id).where(Document.status == "queued", Document.updated_at < cutoff)
                              .order_by(Document.created_at)))  # picked before the update below refreshes updated_at
        stale = db.execute(
            update(Document).where(Document.status == "processing", Document.updated_at < cutoff)
            .values(status="queued").returning(Document.id, Document.case_id, Document.sha256)
        ).all()
        for doc_id, case_id, sha in stale:
            audit_logger.append(db, action="document.requeued", case_id=case_id, source_hash=sha,
                                detail={"document_id": str(doc_id), "reason": "stuck in processing"})
        db.commit()
        ids += [row[0] for row in stale]
    options = ({"date_order": settings.ingest_date_order, "whisper_model": settings.whisper_model,
                "ocr_lang": settings.ocr_lang} if settings.ingestion_provider == "local" else None)
    for doc_id in ids:
        run_ingestion(doc_id, settings.storage_dir, settings.ingestion_provider, options)
    log.info("Reaper processed %s document(s)", len(ids))
    return len(ids)
