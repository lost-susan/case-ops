import hashlib
import math

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import COOKIE_NAME, Principal, current_principal
from app.config import Settings, get_settings
from app.core.clock import utcnow
from app.core.security import (DUMMY_HASH, create_access_token, csrf_token_for, hash_password, needs_rehash,
                               verify_password)
from datetime import datetime, timedelta, timezone
from app.db import get_db
from app.models.orm import LoginThrottle, User
from app.models.schemas import LoginIn, SessionOut, UserOut
from app.services import audit_logger

router = APIRouter(prefix="/auth", tags=["auth"])
GENERIC_FAILURE = "Invalid email or password."


def _register_failure(db: Session, key: str, settings: Settings) -> None:
    """Atomic increment (parallel guesses can't dodge the counter), then lock with exponential back-off."""
    if db.get(LoginThrottle, key) is None:
        try:
            with db.begin_nested():
                db.add(LoginThrottle(key=key, fails=0))
                db.flush()
        except IntegrityError:
            pass  # a parallel request created it first
    db.execute(update(LoginThrottle).where(LoginThrottle.key == key).values(fails=LoginThrottle.fails + 1))
    fails = db.scalar(select(LoginThrottle.fails).where(LoginThrottle.key == key))
    if fails >= settings.login_free_attempts:
        seconds = min(settings.login_max_lock_seconds,
                      settings.login_base_lock_seconds * 2 ** (fails - settings.login_free_attempts))
        db.execute(update(LoginThrottle).where(LoginThrottle.key == key)
                   .values(locked_until=utcnow() + timedelta(seconds=seconds)))


def _cookie_args(settings: Settings) -> dict:
    return {"httponly": True, "secure": settings.cookie_secure, "samesite": settings.cookie_samesite, "path": "/"}


def _session_out(user: User, claims: dict, settings: Settings) -> SessionOut:
    return SessionOut(user=UserOut.model_validate(user), csrf_token=csrf_token_for(claims["jti"], settings),
                      expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc))


@router.post("/login", response_model=SessionOut)
def login(body: LoginIn, response: Response, db: Session = Depends(get_db),
          settings: Settings = Depends(get_settings)) -> SessionOut:
    email = body.email.lower()
    key = hashlib.sha256(email.encode()).hexdigest()
    now = utcnow()

    throttle = db.get(LoginThrottle, key)
    if throttle and throttle.locked_until and throttle.locked_until > now:
        audit_logger.append(db, action="auth.login.blocked", detail={"email_sha256": key[:16]})
        db.commit()
        retry = math.ceil((throttle.locked_until - now).total_seconds())
        raise HTTPException(429, "Too many failed attempts. Try again later.", headers={"Retry-After": str(retry)})

    user = db.scalar(select(User).where(User.email == email))
    password_ok = verify_password(user.password_hash if user else DUMMY_HASH, body.password)  # always runs
    if not (user and password_ok and user.is_active):
        _register_failure(db, key, settings)
        audit_logger.append(db, action="auth.login.failed", detail={"email_sha256": key[:16]})
        db.commit()
        raise HTTPException(401, GENERIC_FAILURE)  # same answer for unknown email, wrong password, disabled account

    if throttle:
        db.delete(throttle)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    token, claims = create_access_token(user.id, user.token_version, settings)
    audit_logger.append(db, action="auth.login", user_id=user.id)
    db.commit()
    response.set_cookie(COOKIE_NAME, token, max_age=settings.access_token_minutes * 60, **_cookie_args(settings))
    return _session_out(user, claims, settings)


@router.get("/me", response_model=SessionOut)
def me(principal: Principal = Depends(current_principal), settings: Settings = Depends(get_settings)) -> SessionOut:
    return _session_out(principal.user, principal.claims, settings)


@router.post("/logout", status_code=204)
def logout(response: Response, principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings)) -> None:
    db.execute(update(User).where(User.id == principal.user.id).values(token_version=User.token_version + 1))
    audit_logger.append(db, action="auth.logout", user_id=principal.user.id)
    db.commit()
    response.delete_cookie(COOKIE_NAME, **_cookie_args(settings))
