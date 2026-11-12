"""Liveness, core readiness, and optional-dependency status.

Readiness covers only dependencies the service cannot work without (DB, Redis, storage).
Gemini/Groq/Gotenberg/IMAP/SMTP are reported separately and never make /ready fail.
"""
import os

import redis
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import CurrentUser, current_user
from app.core.config import get_settings
from app.core.envelope import fail, ok
from app.database.session import get_engine

router = APIRouter(prefix="/health", tags=["health"])


def _check_db() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - report, never raise from a probe
        return False, type(exc).__name__


def _check_redis() -> tuple[bool, str]:
    try:
        redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2).ping()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def _check_storage() -> tuple[bool, str]:
    root = get_settings().storage_root
    try:
        os.makedirs(root, exist_ok=True)
        probe = os.path.join(root, ".ready_probe")
        with open(probe, "w") as fh:
            fh.write("ok")
        os.remove(probe)
        return True, "ok"
    except OSError as exc:
        return False, type(exc).__name__


@router.get("/live")
def live() -> dict:
    return ok({"status": "alive"})


@router.get("/ready")
def ready():
    checks = {"database": _check_db(), "redis": _check_redis(), "storage": _check_storage()}
    details = {name: status for name, (_, status) in checks.items()}
    if all(good for good, _ in checks.values()):
        return ok({"status": "ready", "checks": details})
    return JSONResponse(
        status_code=503, content=fail("not_ready", "Core dependency unavailable", details)
    )


@router.get("/dependencies")
def dependencies(_: CurrentUser = Depends(current_user)) -> dict:
    """Optional, degradable dependencies. Checked for real in later phases."""
    names = ["gemini", "groq", "gotenberg", "imap", "smtp"]
    return ok({name: {"status": "not_configured"} for name in names})
