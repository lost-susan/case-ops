import hmac
import uuid
from dataclasses import dataclass
from typing import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.core.security import csrf_token_for, decode_access_token
from app.db import get_db
from app.models.orm import Case, CaseMember, User

COOKIE_NAME = "co_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ROLE_RANK = {"viewer": 0, "editor": 1, "owner": 2}


@dataclass
class Principal:
    user: User
    claims: dict


@dataclass
class CaseContext:
    case: Case
    role: str
    user: User


def current_principal(request: Request, db: Session = Depends(get_db),
                      settings: Settings = Depends(get_settings)) -> Principal:
    """Authenticates the session cookie and, for every state-changing method, enforces the CSRF header."""
    token = request.cookies.get(COOKIE_NAME)
    claims = decode_access_token(token, settings) if token else None
    if not claims:
        raise HTTPException(401, "Not authenticated")
    try:
        user = db.get(User, uuid.UUID(claims["sub"]))
    except ValueError:
        raise HTTPException(401, "Not authenticated")
    if user is None or not user.is_active or user.token_version != claims["ver"]:
        raise HTTPException(401, "Not authenticated")  # revoked (logout) or disabled
    if request.method not in SAFE_METHODS:
        supplied = request.headers.get("x-csrf-token", "").encode()
        if not hmac.compare_digest(supplied, csrf_token_for(claims["jti"], settings).encode()):
            raise HTTPException(403, "CSRF token missing or invalid")
    return Principal(user, claims)


def case_access(min_role: str = "viewer") -> Callable[..., CaseContext]:
    """Case-scoped authorization. A non-member gets 404 (never confirm another firm's case exists)."""
    def dependency(case_id: uuid.UUID, db: Session = Depends(get_db),
                   principal: Principal = Depends(current_principal)) -> CaseContext:
        row = db.execute(
            select(Case, CaseMember.role).join(CaseMember, CaseMember.case_id == Case.id)
            .where(Case.id == case_id, CaseMember.user_id == principal.user.id)
        ).first()
        if row is None:
            raise HTTPException(404, "Case not found")
        if ROLE_RANK[row.role] < ROLE_RANK[min_role]:
            raise HTTPException(403, "Insufficient permissions for this case")
        return CaseContext(case=row.Case, role=row.role, user=principal.user)
    return dependency
