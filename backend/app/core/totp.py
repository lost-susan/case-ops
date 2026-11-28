"""RFC 6238 time-based one-time passwords (HMAC-SHA1, 30 s step, 6 digits) with encrypted-at-rest secrets.

The secret is encrypted with a key derived from CASEOPS_SECRET_KEY, so a database leak alone does not reveal it.
Rotating CASEOPS_SECRET_KEY therefore requires users to re-enrol MFA (it also invalidates every session)."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

STEP = 30
DIGITS = 6
WINDOW = 1  # accept one step either side for clock drift


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10**DIGITS
    return str(value).zfill(DIGITS)


def verify(secret: str, code: str, last_step: int | None = None, now: float | None = None) -> int | None:
    """The matching time step, or None. A step at or before `last_step` is refused so a code works only once."""
    if not (len(code) == DIGITS and code.isdigit()):
        return None
    current = int((time.time() if now is None else now) // STEP)
    matched = None
    for step in range(current - WINDOW, current + WINDOW + 1):  # constant work: no early exit
        if hmac.compare_digest(_code(secret, step), code) and matched is None:
            matched = step
    if matched is None or (last_step is not None and matched <= last_step):
        return None
    return matched


def provisioning_uri(issuer: str, account: str, secret: str) -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def _fernet(app_secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"caseops-mfa:" + app_secret.encode()).digest()))


def seal(secret: str, app_secret: str) -> str:
    return _fernet(app_secret).encrypt(secret.encode()).decode()


def unseal(token: str, app_secret: str) -> str | None:
    try:
        return _fernet(app_secret).decrypt(token.encode()).decode()
    except InvalidToken:
        return None
