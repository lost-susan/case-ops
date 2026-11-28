import hashlib
import math

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import COOKIE_NAME, Principal, current_principal
from app.config import Settings, get_settings
from app.core import totp
from app.core.clock import utcnow
from app.core.security import (DUMMY_HASH, create_access_token, csrf_token_for, hash_password, needs_rehash,
                               verify_password)
from datetime import datetime, timedelta, timezone
from app.db import get_db
from app.models.orm import LoginThrottle, User
from app.models.schemas import (LoginIn, MfaCodeIn, MfaDisableIn, MfaSetupOut, PasswordChangeIn, SessionOut,
                                UserOut)
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

    if user.mfa_enabled:
        if body.totp is None:
            raise HTTPException(401, "mfa_required")  # only reached with the right password; frontend asks for the code
        secret = totp.unseal(user.mfa_secret or "", settings.secret_key)
        step = totp.verify(secret, body.totp, user.mfa_last_step) if secret else None
        if step is None:
            _register_failure(db, key, settings)  # wrong codes count toward the lockout like wrong passwords
            audit_logger.append(db, action="auth.login.failed", detail={"email_sha256": key[:16], "mfa": True})
            db.commit()
            raise HTTPException(401, GENERIC_FAILURE)
        user.mfa_last_step = step  # a code can be used once

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


def _revoke_all(db: Session, user_id) -> None:
    db.execute(update(User).where(User.id == user_id).values(token_version=User.token_version + 1))


@router.post("/password", status_code=204)
def change_password(body: PasswordChangeIn, response: Response, principal: Principal = Depends(current_principal),
                    db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> None:
    """Change your own password. Every session (including this one) is revoked: sign in again afterwards."""
    user = principal.user
    if not verify_password(user.password_hash, body.current_password):
        audit_logger.append(db, action="auth.password.change_failed", user_id=user.id)
        db.commit()
        raise HTTPException(403, "Current password is incorrect")
    if body.new_password == body.current_password:
        raise HTTPException(422, "Choose a different password")
    user.password_hash = hash_password(body.new_password)
    _revoke_all(db, user.id)
    audit_logger.append(db, action="auth.password.changed", user_id=user.id)
    db.commit()
    response.delete_cookie(COOKIE_NAME, **_cookie_args(settings))


@router.post("/mfa/setup", response_model=MfaSetupOut)
def mfa_setup(principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings)) -> MfaSetupOut:
    """Start enrolment: returns a fresh secret to add to an authenticator app. MFA is off until /mfa/enable."""
    user = principal.user
    if user.mfa_enabled:
        raise HTTPException(409, "MFA is already enabled")
    secret = totp.new_secret()
    user.mfa_secret = totp.seal(secret, settings.secret_key)
    db.commit()
    return MfaSetupOut(secret=secret, otpauth_uri=totp.provisioning_uri("CASE-OPS", user.email, secret))


@router.post("/mfa/enable", status_code=204)
def mfa_enable(body: MfaCodeIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
               settings: Settings = Depends(get_settings)) -> None:
    user = principal.user
    secret = totp.unseal(user.mfa_secret or "", settings.secret_key) if user.mfa_secret else None
    if user.mfa_enabled or not secret:
        raise HTTPException(409, "Start MFA setup first")
    step = totp.verify(secret, body.code)
    if step is None:
        raise HTTPException(422, "That code is not valid")
    user.mfa_enabled, user.mfa_last_step = True, step
    audit_logger.append(db, action="auth.mfa.enabled", user_id=user.id)
    db.commit()


@router.post("/mfa/disable", status_code=204)
def mfa_disable(body: MfaDisableIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db),
                settings: Settings = Depends(get_settings)) -> None:
    user = principal.user
    secret = totp.unseal(user.mfa_secret or "", settings.secret_key) if user.mfa_secret else None
    ok = (user.mfa_enabled and verify_password(user.password_hash, body.password) and secret is not None
          and totp.verify(secret, body.code, user.mfa_last_step) is not None)
    if not ok:
        audit_logger.append(db, action="auth.mfa.disable_failed", user_id=user.id)
        db.commit()
        raise HTTPException(403, "Password or code is incorrect")
    user.mfa_enabled, user.mfa_secret, user.mfa_last_step = False, None, None
    audit_logger.append(db, action="auth.mfa.disabled", user_id=user.id)
    db.commit()
