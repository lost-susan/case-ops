"""Tamper-evident audit ledger: each row stores sha256(prev_hash | fields), forming a hash chain per case."""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.models.audit import AuditLog

GENESIS = "0" * 64


def _canonical(detail: dict[str, Any] | None) -> str:
    return json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def compute_hash(prev_hash: str, seq: int, chain_key: str, case_id, user_id, action: str,
                 source_hash: str | None, detail: dict | None, created_at: datetime) -> str:
    material = "|".join([
        prev_hash, str(seq), chain_key, str(case_id), str(user_id), action, str(source_hash),
        _canonical(detail), created_at.astimezone(timezone.utc).isoformat(),
    ])
    return hashlib.sha256(material.encode()).hexdigest()


def append(db: Session, *, action: str, case_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None,
           source_hash: str | None = None, detail: dict[str, Any] | None = None) -> AuditLog:
    """Add one entry inside the caller's transaction (the caller commits). Serialised per chain so two
    concurrent writers can't fork it; the (chain_key, seq) unique constraint is the backstop."""
    chain_key = str(case_id) if case_id else "global"
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": chain_key})
    last = db.execute(
        select(AuditLog.seq, AuditLog.entry_hash).where(AuditLog.chain_key == chain_key)
        .order_by(AuditLog.seq.desc()).limit(1)
    ).first()
    seq = last.seq + 1 if last else 1
    prev = last.entry_hash if last else GENESIS
    detail = json.loads(_canonical(detail)) if detail is not None else None  # store exactly what is hashed
    created = utcnow()
    entry = AuditLog(
        chain_key=chain_key, seq=seq, case_id=case_id, user_id=user_id, action=action, source_hash=source_hash,
        detail=detail, created_at=created, prev_hash=prev,
        entry_hash=compute_hash(prev, seq, chain_key, case_id, user_id, action, source_hash, detail, created),
    )
    db.add(entry)
    db.flush()
    return entry


def verify_chain(db: Session, chain_key: str) -> dict[str, Any]:
    """Recompute the whole chain. Returns {valid, checked, first_bad_seq}."""
    prev, expected_seq, checked = GENESIS, 1, 0
    rows = db.execute(select(AuditLog).where(AuditLog.chain_key == chain_key).order_by(AuditLog.seq)).scalars()
    for row in rows:
        good = (
            row.seq == expected_seq and row.prev_hash == prev and row.entry_hash == compute_hash(
                row.prev_hash, row.seq, row.chain_key, row.case_id, row.user_id, row.action,
                row.source_hash, row.detail, row.created_at)
        )
        if not good:
            return {"valid": False, "checked": checked, "first_bad_seq": row.seq}
        prev, expected_seq, checked = row.entry_hash, expected_seq + 1, checked + 1
    return {"valid": True, "checked": checked, "first_bad_seq": None}
