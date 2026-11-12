"""Pick the next pending input for a project, oldest first, from domain tables.

No custom event queue: ordering comes from monotonic columns (email inbound_seq / created_at)
so duplicate, late or out-of-order Celery kicks can never reorder or double-process work.
"""
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import ManualReview
from app.services.ingestion_service import pending_text_sources
from app.workflows.state import WorkflowTrigger

AWAIT_MANUAL = "await_manual_review"


@dataclass(frozen=True)
class Pending:
    trigger: WorkflowTrigger
    sort_key: tuple


def resolved_graph_reviews(session: Session, project_id: uuid.UUID) -> list[ManualReview]:
    """Reviews raised INSIDE the graph (workflow_run_id set) that staff have decided."""
    return list(session.execute(
        select(ManualReview).where(
            ManualReview.project_id == project_id, ManualReview.status == "resolved",
            ManualReview.workflow_run_id.is_not(None)).order_by(ManualReview.resolved_at)).scalars())


def next_trigger(session: Session, project_id: uuid.UUID,
                 waiting_at: str | None) -> WorkflowTrigger | None:
    decided = resolved_graph_reviews(session, project_id)
    if decided:  # control lane first
        return {"type": "manual_decision", "ref_id": str(decided[0].id)}
    if waiting_at == AWAIT_MANUAL:
        return None  # client input waits until staff decide
    candidates: list[Pending] = []
    for src in pending_text_sources(session, project_id):
        if src.email_message_id is None:  # email-borne sources are handled with their email
            candidates.append(Pending({"type": "document", "ref_id": str(src.id)},
                                      (src.created_at, 1, str(src.id))))
    from app.workflows import email_queue  # local import: email phase

    candidates.extend(email_queue.pending_emails(session, project_id))
    if not candidates:
        return None
    return min(candidates, key=lambda p: p.sort_key).trigger
