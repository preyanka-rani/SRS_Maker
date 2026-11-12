"""Queued inbound emails for a project, ordered by arrival (inbound_seq).

An email is held back while its own attachments are still being ingested/transcribed, so the
workflow always sees the complete message.
"""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import EmailMessage, ExtractionStatus, InputSource, MailStatus
from app.workflows.queue import Pending

IN_FLIGHT = (ExtractionStatus.pending, ExtractionStatus.processing, ExtractionStatus.transcribing)


def pending_emails(session: Session, project_id: uuid.UUID) -> list[Pending]:
    rows = session.execute(
        select(EmailMessage).where(EmailMessage.project_id == project_id,
                                   EmailMessage.direction == "inbound",
                                   EmailMessage.processing_status.in_([MailStatus.queued,
                                                                       MailStatus.processing]))
        .order_by(EmailMessage.inbound_seq)).scalars().all()
    out: list[Pending] = []
    for m in rows:
        waiting = session.execute(select(InputSource.id).where(
            InputSource.email_message_id == m.id,
            InputSource.extraction_status.in_(IN_FLIGHT)).limit(1)).first()
        if waiting is None:
            out.append(Pending({"type": "email", "ref_id": str(m.id)},
                               (m.created_at, 0, str(m.inbound_seq))))
        else:
            break  # strict arrival order: later emails wait behind this one
    return out
