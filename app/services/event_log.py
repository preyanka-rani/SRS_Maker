"""Append-only activity history (workflow_event_log). Audit only: it never drives workers."""
import uuid

from sqlalchemy.orm import Session

from app.database.models import WorkflowEventLog


def log_event(session: Session, project_id: uuid.UUID, event_type: str, *, ref_type: str | None = None,
              ref_id: str | None = None, run_id: uuid.UUID | None = None,
              task_id: str | None = None, details: dict | None = None) -> None:
    session.add(WorkflowEventLog(project_id=project_id, event_type=event_type, ref_type=ref_type,
                                 ref_id=ref_id, workflow_run_id=run_id, celery_task_id=task_id,
                                 details=details))
    session.flush()
