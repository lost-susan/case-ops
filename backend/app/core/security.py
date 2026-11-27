"""Password hashing (argon2id), session tokens (JWT in an httpOnly cookie) and CSRF tokens."""
import hashlib
import hmac
import uuid
from datetime import timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import Settings
from app.core.clock import utcnow

_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
# Verified when the email is unknown so response time doesn't reveal whether an account exists.
DUMMY_HASH = _hasher.hash("caseops-dummy-password-for-timing")
MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def create_access_token(user_id: uuid.UUID, token_version: int, settings: Settings) -> tuple[str, dict[str, Any]]:
    now = utcnow()
    claims = {
        "sub": str(user_id),
        "ver": token_version,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.access_token_minutes)).timestamp()),
    }
    return jwt.encode(claims, settings.secret_key, algorithm="HS256"), claims


def decode_access_token(token: str, settings: Settings) -> dict[str, Any] | None:
    try:
        claims = jwt.decode(
            token, settings.secret_key, algorithms=["HS256"],  # pin the algorithm: no "alg: none" downgrade
            options={"require": ["exp", "iat", "sub", "jti", "ver"]},
        )
    except jwt.PyJWTError:
        return None
    return claims if isinstance(claims.get("jti"), str) and isinstance(claims.get("ver"), int) else None


def csrf_token_for(jti: str, settings: Settings) -> str:
    """Bound to the session (HMAC of its jti). An attacker's page can't read it: it only comes back in a
    CORS-protected JSON response and must be echoed in the X-CSRF-Token header."""
    return hmac.new(settings.secret_key.encode(), f"csrf:{jti}".encode(), hashlib.sha256).hexdigest()
