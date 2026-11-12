"""Outbound mail: write rows in the same transaction as the state change, dispatch separately.

Semantics (plan S2): SMTP 250 = sent; 4xx/network = retry with backoff then dead; 5xx = dead;
a worker crash after 250 but before commit leaves a stale `sending` row that is re-sent with the
SAME Message-ID (at most one duplicate, documented).
"""
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import EmailOutbox, OutboxStatus, Project
from app.integrations.email.smtp_adapter import (
    OutgoingMail,
    SmtpAdapter,
    SmtpPermanentError,
    SmtpTemporaryError,
)
from app.integrations.storage.local import LocalStorage
from app.services import event_log, issue_service

BACKOFF_MINUTES = (1, 5, 30, 120, 360)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def subject_for(project: Project, text: str) -> str:
    """Fixed subject convention: the token makes thread binding reliable when headers are stripped."""
    return f"[{project.project_token}] {project.title} – {text}"


def enqueue(session: Session, settings: Settings, project: Project, *, kind: str, to: list[str],
            subject: str, body_text: str, idempotency_key: str, in_reply_to: str | None = None,
            references: list[str] | None = None, attachments: list[dict] | None = None,
            srs_version_id: uuid.UUID | None = None, cc: list[str] | None = None) -> EmailOutbox:
    """Idempotent on `idempotency_key`."""
    existing = session.execute(select(EmailOutbox).where(
        EmailOutbox.idempotency_key == idempotency_key)).scalar_one_or_none()
    if existing:
        return existing
    domain = (settings.mail_from_address or "srs.local").split("@")[-1]
    row = EmailOutbox(
        project_id=project.id, kind=kind, srs_version_id=srs_version_id, to_addresses=to,
        cc_addresses=cc or None, subject=subject, body_text=body_text, attachments=attachments,
        message_id=f"<{uuid.uuid4().hex}@{domain}>", in_reply_to=in_reply_to,
        references=references or None, idempotency_key=idempotency_key,
        status=OutboxStatus.pending, attempts=0, next_attempt_at=_now())
    session.add(row)
    session.flush()
    event_log.log_event(session, project.id, "email_queued", ref_type="outbox", ref_id=str(row.id),
                        details={"kind": kind})
    return row


def _load_attachments(storage: LocalStorage, specs: list[dict] | None) -> tuple:
    out = []
    for spec in specs or []:
        data = storage.path(spec["path"]).read_bytes()
        if spec.get("sha256") and hashlib.sha256(data).hexdigest() != spec["sha256"]:
            raise SmtpPermanentError("attachment integrity check failed")
        out.append((spec["filename"], spec["mime"], data))
    return tuple(out)


def release_stale(session: Session, settings: Settings) -> int:
    cutoff = _now() - timedelta(minutes=settings.outbox_stale_sending_minutes)
    stale = session.execute(select(EmailOutbox).where(
        EmailOutbox.status == OutboxStatus.sending,
        EmailOutbox.sending_started_at < cutoff).with_for_update(skip_locked=True)).scalars().all()
    for row in stale:
        row.status = OutboxStatus.failed
        row.next_attempt_at = _now()
    return len(stale)


def dispatch_due(session_factory, storage: LocalStorage, settings: Settings,
                 smtp: SmtpAdapter | None = None, limit: int = 20) -> dict[str, int]:
    """Send due rows one at a time, committing between steps so a crash never loses state."""
    counts = {"sent": 0, "retry": 0, "dead": 0}
    with session_factory() as s:
        release_stale(s, settings)
    smtp = smtp or SmtpAdapter(settings)
    for _ in range(limit):
        with session_factory() as s:
            row = s.execute(select(EmailOutbox).where(
                or_(EmailOutbox.status == OutboxStatus.pending, EmailOutbox.status == OutboxStatus.failed),
                EmailOutbox.next_attempt_at <= _now()).order_by(EmailOutbox.created_at)
                .with_for_update(skip_locked=True).limit(1)).scalar_one_or_none()
            if row is None:
                break
            row.status, row.sending_started_at, row.attempts = (
                OutboxStatus.sending, _now(), row.attempts + 1)
            row_id = row.id
        with session_factory() as s:
            row = s.get(EmailOutbox, row_id)
            outcome = _send_one(s, storage, settings, smtp, row)
            counts[outcome] += 1
    return counts


def _send_one(s: Session, storage: LocalStorage, settings: Settings, smtp: SmtpAdapter,
              row: EmailOutbox) -> str:
    try:
        mail = OutgoingMail(
            message_id=row.message_id, to=tuple(row.to_addresses), cc=tuple(row.cc_addresses or ()),
            subject=row.subject, body_text=row.body_text, body_html=row.body_html,
            in_reply_to=row.in_reply_to, references=tuple(row.references or ()),
            attachments=_load_attachments(storage, row.attachments), outbox_id=str(row.id))
        code = smtp.send(mail)
    except SmtpTemporaryError as exc:
        row.last_smtp_code, row.last_error = exc.code, str(exc)[:500]
        if row.attempts >= settings.outbox_max_attempts:
            return _mark_dead(s, row)
        row.status = OutboxStatus.failed
        row.next_attempt_at = _now() + timedelta(
            minutes=BACKOFF_MINUTES[min(row.attempts - 1, len(BACKOFF_MINUTES) - 1)])
        return "retry"
    except (SmtpPermanentError, OSError, ValueError) as exc:
        row.last_smtp_code, row.last_error = getattr(exc, "code", None), str(exc)[:500]
        return _mark_dead(s, row)
    row.status, row.sent_at, row.last_smtp_code = OutboxStatus.sent, _now(), code
    if row.kind in ("review", "revised_review") and row.srs_version_id:
        from app.services import srs_service  # local import: srs_service depends on this module

        srs_service.mark_version_sent(s, row.srs_version_id)
    event_log.log_event(s, row.project_id, "email_sent", ref_type="outbox", ref_id=str(row.id),
                        details={"kind": row.kind})
    return "sent"


def _mark_dead(s: Session, row: EmailOutbox) -> str:
    row.status = OutboxStatus.dead
    issue_service.open_manual_review(s, row.project_id, "OUTBOX_DEAD", details=row.last_error,
                                     entity_type="outbox", entity_id=str(row.id))
    return "dead"


def mark_bounced(session: Session, outbox_id: str, reason: str) -> None:
    row = session.get(EmailOutbox, uuid.UUID(outbox_id))
    if row is None or row.status == OutboxStatus.bounced:
        return
    row.status, row.bounced_at, row.bounce_reason = OutboxStatus.bounced, _now(), reason[:500]
    issue_service.open_manual_review(session, row.project_id, "EMAIL_BOUNCED", details=reason[:500],
                                     entity_type="outbox", entity_id=str(row.id))
