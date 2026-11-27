"""Signed callbacks from an external ingestion worker. HMAC-SHA256 over '<timestamp>.<raw body>', 5-minute window."""
import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models.orm import Document
from app.models.schemas import WebhookIn
from app.services import audit_logger

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
MAX_SKEW_SECONDS = 300


def _verify(request_headers, body: bytes, secret: str) -> None:
    try:
        ts = int(request_headers.get("x-timestamp", ""))
    except ValueError:
        raise HTTPException(401, "Invalid signature")
    sig = request_headers.get("x-signature", "")
    expected = "sha256=" + hmac.new(secret.encode(), str(ts).encode() + b"." + body, hashlib.sha256).hexdigest()
    if abs(time.time() - ts) > MAX_SKEW_SECONDS or not hmac.compare_digest(sig.encode(), expected.encode()):
        raise HTTPException(401, "Invalid signature")


def _apply(db: Session, payload: WebhookIn) -> None:
    changed = db.execute(
        update(Document).where(Document.id == payload.document_id,
                               Document.status.in_(("queued", "processing", "awaiting_extraction")))
        .values(status=payload.status, ocr_confidence=payload.confidence)
    ).rowcount
    if changed != 1:
        raise HTTPException(409, "Document not found or already finalised")
    doc = db.get(Document, payload.document_id)
    audit_logger.append(db, action="document.webhook_update", case_id=doc.case_id, source_hash=doc.sha256,
                        detail={"document_id": str(doc.id), "status": payload.status})
    db.commit()


@router.post("/ingestion", status_code=204)
async def ingestion_callback(request: Request, db: Session = Depends(get_db),
                             settings: Settings = Depends(get_settings)) -> None:
    body = await request.body()
    _verify(request.headers, body, settings.webhook_secret)  # authenticate BEFORE parsing anything
    try:
        payload = WebhookIn.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "Invalid payload")
    await run_in_threadpool(_apply, db, payload)
