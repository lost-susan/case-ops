"""Administrator-only user management. There is no self-registration; accounts are created here (or via the CLI)."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import Principal, current_principal
from app.core.security import hash_password
from app.db import get_db
from app.models.orm import LoginThrottle, User
from app.models.schemas import PasswordResetIn, UserAdminOut, UserCreate
from app.services import audit_logger

router = APIRouter(prefix="/users", tags=["users"])


def admin_only(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.user.role != "admin":
        raise HTTPException(403, "Administrators only")
    return principal


def _target(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.get("", response_model=list[UserAdminOut])
def list_users(admin: Principal = Depends(admin_only), db: Session = Depends(get_db)):
    return db.scalars(select(User).order_by(User.email)).all()


@router.post("", response_model=UserAdminOut, status_code=201)
def create_user(body: UserCreate, admin: Principal = Depends(admin_only), db: Session = Depends(get_db)):
    user = User(email=body.email, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A user with this email already exists")
    audit_logger.append(db, action="user.created", user_id=admin.user.id,
                        detail={"target": str(user.id), "role": user.role})
    db.commit()
    return user


@router.post("/{user_id}/disable", response_model=UserAdminOut)
def disable_user(user_id: uuid.UUID, admin: Principal = Depends(admin_only), db: Session = Depends(get_db)):
    user = _target(db, user_id)
    if user.id == admin.user.id:
        raise HTTPException(409, "You cannot disable your own account")
    user.is_active = False
    user.token_version += 1  # signs them out everywhere immediately
    audit_logger.append(db, action="user.disabled", user_id=admin.user.id, detail={"target": str(user.id)})
    db.commit()
    return user


@router.post("/{user_id}/enable", response_model=UserAdminOut)
def enable_user(user_id: uuid.UUID, admin: Principal = Depends(admin_only), db: Session = Depends(get_db)):
    user = _target(db, user_id)
    user.is_active = True
    audit_logger.append(db, action="user.enabled", user_id=admin.user.id, detail={"target": str(user.id)})
    db.commit()
    return user


@router.post("/{user_id}/reset-password", status_code=204)
def reset_password(user_id: uuid.UUID, body: PasswordResetIn, admin: Principal = Depends(admin_only),
                   db: Session = Depends(get_db)) -> None:
    """Set a new password (for a forgotten one). Their sessions are revoked and any login lockout is cleared."""
    user = _target(db, user_id)
    user.password_hash = hash_password(body.new_password)
    user.token_version += 1
    audit_logger.append(db, action="user.password_reset", user_id=admin.user.id, detail={"target": str(user.id)})
    db.commit()


@router.post("/{user_id}/reset-mfa", status_code=204)
def reset_mfa(user_id: uuid.UUID, admin: Principal = Depends(admin_only), db: Session = Depends(get_db)) -> None:
    """For a lost authenticator: removes MFA so the user can sign in with their password and enrol again."""
    user = _target(db, user_id)
    user.mfa_enabled, user.mfa_secret, user.mfa_last_step = False, None, None
    user.token_version += 1
    audit_logger.append(db, action="user.mfa_reset", user_id=admin.user.id, detail={"target": str(user.id)})
    db.commit()
