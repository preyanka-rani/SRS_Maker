"""Uniform API response envelope: {success, data, error, meta}."""
from typing import Any

from pydantic import BaseModel


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}


class Envelope(BaseModel):
    success: bool
    data: Any = None
    error: ErrorBody | None = None
    meta: dict[str, Any] | None = None


def ok(data: Any = None, meta: dict[str, Any] | None = None) -> dict:
    return Envelope(success=True, data=data, meta=meta).model_dump(mode="json")


def fail(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    body = ErrorBody(code=code, message=message, details=details or {})
    return Envelope(success=False, error=body).model_dump(mode="json")
