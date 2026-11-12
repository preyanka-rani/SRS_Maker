"""Optional push intake: a mail gateway can POST a raw RFC 822 message instead of IMAP polling.
Disabled unless WEBHOOK_SHARED_SECRET is set; the secret is compared in constant time."""
import hmac

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.envelope import ok
from app.core.errors import AppError, NotFoundError
from app.database.session import get_session
from app.integrations.storage.local import LocalStorage
from app.services import email_service
from app.workers import tasks

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
MAX_BYTES = 50 * 1024 * 1024


class Unauthorized(AppError):
    status_code, code = 401, "unauthorized"


@router.post("/email/inbound", status_code=202)
async def inbound_email(request: Request, session: Session = Depends(get_session),
                        x_webhook_secret: str = Header(default="")) -> dict:
    settings = get_settings()
    secret = settings.webhook_shared_secret
    if secret is None:
        raise NotFoundError("Not found")
    if not hmac.compare_digest(x_webhook_secret.encode(), secret.get_secret_value().encode()):
        raise Unauthorized("Invalid webhook secret")
    raw = await request.body()
    if not raw or len(raw) > MAX_BYTES:
        raise Unauthorized("Invalid message size")
    outcome = email_service.ingest_raw_email(session, LocalStorage(settings.storage_path), settings, raw)
    session.commit()
    tasks.handle_poll_outcomes([outcome])
    return ok({"status": outcome.status})
