from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_ph = PasswordHasher()

ALGORITHM = "HS256"
TokenType = Literal["access", "refresh", "email_verify"]
EMAIL_VERIFY_TTL = timedelta(days=2)


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def _create_token(sub: str, role: str, token_type: TokenType, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_access_token(sub: str, role: str) -> str:
    return _create_token(sub, role, "access", timedelta(minutes=settings.access_token_ttl_min))


def create_refresh_token(sub: str, role: str) -> str:
    return _create_token(sub, role, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def create_email_verify_token(sub: str, role: str) -> str:
    return _create_token(sub, role, "email_verify", EMAIL_VERIFY_TTL)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode + validate a JWT. Raises jwt.InvalidTokenError on any problem."""
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    return payload
