from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
import uuid

from app.api.deps import CaseContext, case_access
from app.config import Settings, get_settings
from app.db import get_db
from app.models.orm import Document
from app.models.schemas import DocumentOut
from app.services import audit_logger
from app.services.storage import ALLOWED, LocalStorage, UploadError, extension_of, sanitize_filename
from app.services.task_router import enqueue_ingestion

router = APIRouter(prefix="/cases/{case_id}/documents", tags=["documents"])


@router.post("", response_model=DocumentOut, status_code=202)
def upload_document(background: BackgroundTasks, file: UploadFile = File(...),
                    ctx: CaseContext = Depends(case_access("editor")), db: Session = Depends(get_db),
                    settings: Settings = Depends(get_settings)):
    filename = sanitize_filename(file.filename or "")
    if not filename:
        raise HTTPException(422, "A file name is required")
    rule = ALLOWED.get(extension_of(filename))  # the client-supplied content type is never trusted
    if rule is None:
        raise HTTPException(415, "Unsupported file type. Allowed: pdf, txt, wav, mp3, jpg, png")
    kind, content_type = rule

    storage = LocalStorage(settings.storage_dir)
    try:
        key, size, sha256 = storage.save(file.file, kind=kind, max_bytes=settings.max_upload_bytes)
    except UploadError as e:
        raise HTTPException(e.status, e.message)

    try:
        if db.scalar(select(Document.id).where(Document.case_id == ctx.case.id, Document.sha256 == sha256)):
            raise HTTPException(409, "This exact file was already uploaded to the case")
        doc = Document(case_id=ctx.case.id, filename=filename, kind=kind, content_type=content_type, size_bytes=size,
                       sha256=sha256, storage_key=key, status="queued", uploaded_by=ctx.user.id)
        db.add(doc)
        db.flush()
        audit_logger.append(db, action="document.uploaded", case_id=ctx.case.id, user_id=ctx.user.id,
                            source_hash=sha256, detail={"document_id": str(doc.id), "filename": filename,
                                                        "kind": kind, "size_bytes": size})
        db.commit()
    except (HTTPException, IntegrityError):
        db.rollback()
        storage.delete(key)  # never leave orphaned evidence files behind
        raise HTTPException(409, "This exact file was already uploaded to the case")
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    options = ({"date_order": settings.ingest_date_order, "whisper_model": settings.whisper_model,
                "ocr_lang": settings.ocr_lang} if settings.ingestion_provider == "local" else None)
    enqueue_ingestion(background, doc.id, settings.storage_dir, settings.ingestion_provider, options)
    return doc


def _get(db: Session, ctx: CaseContext, document_id: uuid.UUID) -> Document:
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.case_id == ctx.case.id))
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


@router.get("/{document_id}", response_model=DocumentOut)
def get_document(document_id: uuid.UUID, ctx: CaseContext = Depends(case_access("viewer")),
                 db: Session = Depends(get_db)):
    return _get(db, ctx, document_id)


@router.get("/{document_id}/download")
def download_document(document_id: uuid.UUID, ctx: CaseContext = Depends(case_access("viewer")),
                      db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    doc = _get(db, ctx, document_id)
    path = LocalStorage(settings.storage_dir).path(doc.storage_key)
    if not path.is_file():
        raise HTTPException(404, "File is missing from storage")
    audit_logger.append(db, action="document.downloaded", case_id=ctx.case.id, user_id=ctx.user.id,
                        source_hash=doc.sha256, detail={"document_id": str(doc.id)})
    db.commit()
    return FileResponse(path, media_type=doc.content_type, filename=doc.filename, content_disposition_type="attachment")
