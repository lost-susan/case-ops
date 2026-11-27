import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Index, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.db import Base, UTCDateTime


class AuditLog(Base):
    """Append-only, hash-chained ledger. One chain per case (plus a 'global' chain for auth events).
    Each entry commits to the previous entry's hash, so editing or deleting history is detectable."""
    __tablename__ = "audit_log"
    __table_args__ = (
        UniqueConstraint("chain_key", "seq", name="uq_audit_chain_seq"),
        Index("ix_audit_case_created", "case_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    chain_key: Mapped[str] = mapped_column(String(40))
    seq: Mapped[int] = mapped_column(Integer)
    case_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # no FK: history must outlive deletions
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    action: Mapped[str] = mapped_column(String(120))
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    prev_hash: Mapped[str] = mapped_column(String(64))
    entry_hash: Mapped[str] = mapped_column(String(64))
