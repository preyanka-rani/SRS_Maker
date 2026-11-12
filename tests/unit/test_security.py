import pytest

from app.core.errors import UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_roundtrip():
    hashed = hash_password("s3cret!")
    assert hashed != "s3cret!"
    assert verify_password("s3cret!", hashed)
    assert not verify_password("wrong", hashed)


def test_token_roundtrip():
    token = create_access_token("user-1", "admin")
    claims = decode_access_token(token)
    assert claims["sub"] == "user-1"
    assert claims["role"] == "admin"


def test_expired_token_rejected():
    token = create_access_token("user-1", "admin", ttl_minutes=-1)
    with pytest.raises(UnauthorizedError):
        decode_access_token(token)


def test_garbage_token_rejected():
    with pytest.raises(UnauthorizedError):
        decode_access_token("not-a-jwt")
