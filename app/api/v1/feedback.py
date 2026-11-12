"""Client feedback and approval history (read-only views)."""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_member_project
from app.core.envelope import ok
from app.database.models import Approval, FeedbackChangeItem, FeedbackRecord
from app.database.session import get_session

router = APIRouter(prefix="/projects/{project_id}", tags=["feedback"])


@router.get("/feedback")
def list_feedback(project_id: uuid.UUID, session: Session = Depends(get_session),
                  user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    records = session.execute(select(FeedbackRecord).where(FeedbackRecord.project_id == project_id)
                              .order_by(FeedbackRecord.created_at)).scalars().all()
    out = []
    for r in records:
        items = session.execute(select(FeedbackChangeItem).where(
            FeedbackChangeItem.feedback_record_id == r.id)).scalars().all()
        out.append({"id": str(r.id), "category": r.category.value, "confidence": r.confidence,
                    "status": r.status, "created_at": r.created_at.isoformat(),
                    "changes": [{"action": i.action, "status": i.status, "title": i.proposed_title,
                                 "statement": i.proposed_statement} for i in items]})
    return ok(out)


@router.get("/approvals")
def list_approvals(project_id: uuid.UUID, session: Session = Depends(get_session),
                   user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    rows = session.execute(select(Approval).where(Approval.project_id == project_id)
                           .order_by(Approval.created_at)).scalars().all()
    return ok([{"id": str(a.id), "outcome": a.outcome, "checks": a.checks,
                "srs_version_id": str(a.srs_version_id),
                "final_srs_version_id": str(a.final_srs_version_id) if a.final_srs_version_id else None,
                "created_at": a.created_at.isoformat()} for a in rows])
