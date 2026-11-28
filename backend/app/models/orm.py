"""Relational model. The PRD invariant (every insight links to raw source proof) is enforced by the schema:
events, contradictions and deadlines all carry NOT NULL foreign keys to a document line."""
import uuid
from datetime import date, datetime

from sqlalchemy import (JSON, BigInteger, Boolean, CheckConstraint, Date, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint, Uuid)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.db import Base, UTCDateTime

ROLES = ("admin", "attorney", "paralegal")
MEMBER_ROLES = ("owner", "editor", "viewer")
DOC_KINDS = ("pdf", "txt", "wav", "mp3", "jpg", "png")
DOC_STATUSES = ("queued", "processing", "awaiting_extraction", "parsed", "transcribed", "failed")
EVENT_STATUSES = ("verified", "conflict", "pending")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", ROLES), name="ck_users_role"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="attorney")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_version: Mapped[int] = mapped_column(Integer, default=0)  # bump to revoke every session of this user
    mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Fernet-sealed TOTP secret
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    mfa_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # last accepted TOTP step (no replay)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LoginThrottle(Base):
    """Failed-login counter keyed by sha256(email), so unknown emails are throttled exactly like real ones."""
    __tablename__ = "login_throttle"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    fails: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    docket: Mapped[str] = mapped_column(String(64), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CaseMember(Base):
    __tablename__ = "case_members"
    __table_args__ = (CheckConstraint(_in("role", MEMBER_ROLES), name="ck_case_members_role"),)

    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="viewer")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(_in("kind", DOC_KINDS), name="ck_documents_kind"),
        CheckConstraint(_in("status", DOC_STATUSES), name="ck_documents_status"),
        UniqueConstraint("case_id", "sha256", name="uq_documents_case_sha"),
        Index("ix_documents_case_created", "case_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(8))
    content_type: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_key: Mapped[str] = mapped_column(String(32), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)  # stuck-job detection


class DocLine(Base):
    """One extracted line of text (OCR line, chat line, or transcript segment): the unit of proof."""
    __tablename__ = "doc_lines"
    __table_args__ = (UniqueConstraint("document_id", "page", "idx", name="uq_doc_lines_pos"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page: Mapped[int] = mapped_column(Integer, default=1)
    idx: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [x1, y1, x2, y2]
    start_sec: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        CheckConstraint(_in("status", EVENT_STATUSES), name="ck_events_status"),
        Index("ix_events_case_time", "case_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    actors: Mapped[list] = mapped_column(JSON, default=list)
    line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doc_lines.id", ondelete="RESTRICT"))  # proof, required
    status: Mapped[str] = mapped_column(String(12), default="pending")


class Contradiction(Base):
    __tablename__ = "contradictions"
    __table_args__ = (
        CheckConstraint("line_a_id <> line_b_id", name="ck_contradictions_distinct"),
        CheckConstraint(_in("origin", ("manual", "ai")), name="ck_contradictions_origin"),
        CheckConstraint(_in("review_state", ("unreviewed", "confirmed", "dismissed")), name="ck_contradictions_review"),
        UniqueConstraint("case_id", "line_a_id", "line_b_id", name="uq_contradictions_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text)
    line_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doc_lines.id", ondelete="RESTRICT"))
    line_b_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doc_lines.id", ondelete="RESTRICT"))
    origin: Mapped[str] = mapped_column(String(8), default="manual")  # "ai" rows are proposals until a lawyer reviews
    review_state: Mapped[str] = mapped_column(String(12), default="confirmed")


class Deadline(Base):
    __tablename__ = "deadlines"
    __table_args__ = (
        CheckConstraint(
            "(rule_days IS NOT NULL AND rule_months IS NULL) OR (rule_days IS NULL AND rule_months IS NOT NULL)",
            name="ck_deadlines_one_rule",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    citation: Mapped[str] = mapped_column(String(200))
    rule_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_label: Mapped[str] = mapped_column(String(120))
    trigger_date: Mapped[date] = mapped_column(Date)
    trigger_line_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("doc_lines.id", ondelete="RESTRICT"))
    rule_verified: Mapped[bool] = mapped_column(Boolean, default=False)  # true only after counsel confirms the rule
