"""Password hashing (argon2) and JWT helpers."""
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings
from app.core.errors import UnauthorizedError

_hasher = PasswordHasher()
_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def create_access_token(subject: str, role: str, ttl_minutes: int | None = None) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    ttl = ttl_minutes if ttl_minutes is not None else settings.jwt_ttl_minutes
    payload = {"sub": subject, "role": role, "iat": now, "exp": now + timedelta(minutes=ttl)}
    return jwt.encode(payload, settings.app_secret_key.get_secret_value(), algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(
            token, get_settings().app_secret_key.get_secret_value(), algorithms=[_ALGORITHM]
        )
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc
