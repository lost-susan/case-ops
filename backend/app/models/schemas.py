"""Request/response contracts. Requests forbid unknown fields and bound every string/number."""
import re
import uuid
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- auth ----------
class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # passwords are NOT whitespace-stripped
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    totp: str | None = Field(None, pattern=r"^\d{6}$")  # required only for accounts with MFA enabled

    @field_validator("email", mode="before")
    @classmethod
    def _trim_email(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("password")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        if _CONTROL.search(v):
            raise ValueError("Unsupported characters")
        return v


class UserOut(Out):
    id: uuid.UUID
    email: str
    role: str
    mfa_enabled: bool = False


def _strong(v: str) -> str:
    if _CONTROL.search(v):
        raise ValueError("Unsupported characters")
    if len(v) < 12:
        raise ValueError("Password must be at least 12 characters")
    return v


class PasswordChangeIn(Strict):
    model_config = ConfigDict(extra="forbid")  # passwords are not whitespace-stripped
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _check_new_password(cls, v: str) -> str:
        return _strong(v)


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: Literal["admin", "attorney", "paralegal"]
    password: str = Field(min_length=12, max_length=128)

    @field_validator("password")
    @classmethod
    def _check_password(cls, v: str) -> str:
        return _strong(v)

    @field_validator("email", mode="before")
    @classmethod
    def _trim(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class PasswordResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _check_new_password(cls, v: str) -> str:
        return _strong(v)


class UserAdminOut(Out):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    mfa_enabled: bool
    created_at: datetime


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class MfaCodeIn(Strict):
    code: str = Field(pattern=r"^\d{6}$")


class MfaDisableIn(Strict):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(pattern=r"^\d{6}$")


class MemberAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    role: Literal["owner", "editor", "viewer"] = "viewer"

    @field_validator("email", mode="before")
    @classmethod
    def _trim(cls, v):
        return v.strip().lower() if isinstance(v, str) else v


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str
    expires_at: datetime


# ---------- cases ----------
class CaseCreate(Strict):
    title: str = Field(min_length=1, max_length=200)
    docket: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/\- ]{0,63}$")
    timezone: str = Field("America/New_York", max_length=64)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("Unknown IANA timezone")
        return v


class CaseOut(Out):
    id: uuid.UUID
    title: str
    docket: str
    timezone: str
    created_at: datetime


class DocumentOut(Out):
    id: uuid.UUID
    filename: str
    kind: str
    size_bytes: int
    sha256: str
    status: str
    ocr_confidence: float | None
    created_at: datetime


class SourceRef(BaseModel):
    line_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    locator: str  # "p. 2", "line 142" or "00:42"
    quote: str


class EventOut(BaseModel):
    id: uuid.UUID
    title: str
    occurred_at: datetime
    actors: list[str]
    status: str
    source: SourceRef


class ContradictionOut(BaseModel):
    id: uuid.UUID
    title: str
    summary: str
    claim_a: SourceRef
    claim_b: SourceRef
    origin: Literal["manual", "ai"]  # "ai" = proposed by a model, not a finding, until reviewed
    review_state: Literal["unreviewed", "confirmed", "dismissed"]


class ContradictionCreate(Strict):
    title: str = Field(min_length=1, max_length=300)
    summary: str = Field(min_length=1, max_length=4000)
    line_a_id: uuid.UUID
    line_b_id: uuid.UUID

    @model_validator(mode="after")
    def _distinct(self):
        if self.line_a_id == self.line_b_id:
            raise ValueError("A contradiction needs two different source lines")
        return self


class ReviewIn(Strict):
    decision: Literal["confirm", "dismiss"]


class SearchHitOut(BaseModel):
    source: SourceRef
    snippet: str


class SearchOut(BaseModel):
    query: str
    total: int
    hits: list[SearchHitOut]


class DeadlineCreate(Strict):
    title: str = Field(min_length=1, max_length=200)
    citation: str = Field(min_length=1, max_length=200)
    rule_days: int | None = Field(None, ge=1, le=3650)
    rule_months: int | None = Field(None, ge=1, le=120)
    trigger_label: str = Field(min_length=1, max_length=120)
    trigger_date: date
    trigger_line_id: uuid.UUID

    @model_validator(mode="after")
    def _one_rule(self):
        if (self.rule_days is None) == (self.rule_months is None):
            raise ValueError("Provide exactly one of rule_days or rule_months")
        return self


class DeadlineOut(BaseModel):
    id: uuid.UUID
    title: str
    citation: str
    trigger_label: str
    trigger_date: date
    due_date: date
    days_left: int
    urgency: str
    formula: str
    rule_verified: bool
    source: SourceRef


class ProofLineOut(BaseModel):
    idx: int
    text: str
    bbox: list[int] | None
    start_sec: float | None


class ProofOut(BaseModel):
    document: DocumentOut
    page: int
    highlight_index: int
    confidence: float | None
    lines: list[ProofLineOut]


class AuditOut(Out):
    seq: int
    action: str
    user_id: uuid.UUID | None
    source_hash: str | None
    detail: dict | None
    created_at: datetime
    prev_hash: str
    entry_hash: str


class SummaryOut(BaseModel):
    indexed_files: int
    mean_confidence: float | None
    conflicts: int
    next_deadline: DeadlineOut | None


class WebhookIn(Strict):
    document_id: uuid.UUID
    status: Literal["parsed", "transcribed", "failed"]
    confidence: float | None = Field(None, ge=0, le=100)
