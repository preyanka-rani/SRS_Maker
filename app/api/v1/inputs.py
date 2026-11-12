"""Mailbox operations for staff: outbox view/retry, unmatched mail, manual poll."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_roles
from app.core.envelope import ok
from app.core.errors import ConflictError, NotFoundError
from app.database.models import (
    EmailMessage,
    EmailOutbox,
    MailStatus,
    OutboxStatus,
    UserRole,
)
from app.database.session import get_session
from app.workers import tasks

router = APIRouter(prefix="/email", tags=["email"])
_admins = require_roles(UserRole.admin)


@router.get("/outbox")
def outbox(status: str | None = None, session: Session = Depends(get_session),
           _: CurrentUser = Depends(_admins)) -> dict:
    query = select(EmailOutbox).order_by(EmailOutbox.created_at.desc()).limit(200)
    if status:
        query = query.where(EmailOutbox.status == OutboxStatus(status))
    return ok([{"id": str(o.id), "project_id": str(o.project_id), "kind": o.kind, "to": o.to_addresses,
                "subject": o.subject, "status": o.status.value, "attempts": o.attempts,
                "last_error": o.last_error} for o in session.execute(query).scalars()])


@router.post("/outbox/{outbox_id}/retry", status_code=202)
def retry(outbox_id: uuid.UUID, session: Session = Depends(get_session),
          _: CurrentUser = Depends(_admins)) -> dict:
    row = session.get(EmailOutbox, outbox_id)
    if row is None:
        raise NotFoundError("Outbox item not found")
    if row.status not in (OutboxStatus.dead, OutboxStatus.bounced):
        raise ConflictError("Only dead or bounced mail can be retried")
    row.status, row.attempts = OutboxStatus.failed, 0
    return ok({"id": str(row.id), "status": "failed"})


@router.get("/unmatched")
def unmatched(session: Session = Depends(get_session), _: CurrentUser = Depends(current_user)) -> dict:
    rows = session.execute(select(EmailMessage).where(
        EmailMessage.processing_status == MailStatus.unmatched).order_by(
        EmailMessage.created_at.desc()).limit(100)).scalars()
    return ok([{"id": str(m.id), "from": m.from_address, "subject": m.subject,
                "reason": m.match_failure_reason} for m in rows])


@router.post("/poll", status_code=202)
def poll_now(_: CurrentUser = Depends(_admins)) -> dict:
    """Poll the mailbox right now instead of waiting for the next scheduled run."""
    return ok({"projects_touched": tasks.poll_imap()})
