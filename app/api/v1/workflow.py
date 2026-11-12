"""Workflow status, activity history, requeue, and the manual-review queue."""
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_member_project, require_roles
from app.core.envelope import ok
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.database.models import ManualReview, Project, ProjectMember, UserRole, WorkflowEventLog, WorkflowRun
from app.database.session import get_session
from app.schemas.requirements import ManualDecision, ManualReviewOut, ReasonBody
from app.services import audit_service, event_log
from app.services.ingestion_service import pending_text_sources
from app.workers.tasks import kick_workflow

router = APIRouter(tags=["workflow"])
_writers = require_roles(UserRole.admin, UserRole.analyst)
_admins = require_roles(UserRole.admin)

DECISIONS_FOR_GRAPH_REVIEWS = {"regenerate", "accept_approval", "reject_approval", "apply_changes",
                               "send_reply", "dismiss"}
DECISIONS_FOR_STANDALONE = {"dismiss", "resolved", "acknowledge"}


@router.post("/projects/{project_id}/start", status_code=202)
def start(project_id: uuid.UUID, session: Session = Depends(get_session),
          user: CurrentUser = Depends(_writers)) -> dict:
    project = get_member_project(session, user, project_id)
    pending = pending_text_sources(session, project_id)
    event_log.log_event(session, project_id, "start_requested", details={"pending": len(pending)})
    session.commit()
    kick_workflow(project.id)
    return ok({"pending_inputs": len(pending)})


@router.get("/projects/{project_id}/workflow")
def workflow_status(project_id: uuid.UUID, session: Session = Depends(get_session),
                    user: CurrentUser = Depends(current_user)) -> dict:
    project = get_member_project(session, user, project_id)
    runs = session.execute(select(WorkflowRun).where(WorkflowRun.project_id == project_id)
                           .order_by(WorkflowRun.started_at.desc()).limit(10)).scalars().all()
    open_reviews = session.execute(select(ManualReview).where(
        ManualReview.project_id == project_id, ManualReview.status == "open")).scalars().all()
    return ok({
        "project_status": project.status.value,
        "pending_inputs": len(pending_text_sources(session, project_id)),
        "open_manual_reviews": len(open_reviews),
        "runs": [{"id": str(r.id), "trigger": r.trigger_type, "status": r.status,
                  "current_node": r.current_node, "error": r.error,
                  "started_at": r.started_at.isoformat()} for r in runs]})


@router.get("/projects/{project_id}/activity")
def activity(project_id: uuid.UUID, type: str | None = None, page: int = 1,
             session: Session = Depends(get_session), user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    query = select(WorkflowEventLog).where(WorkflowEventLog.project_id == project_id)
    if type:
        query = query.where(WorkflowEventLog.event_type == type)
    rows = session.execute(query.order_by(WorkflowEventLog.id.desc()).limit(100)
                           .offset((max(page, 1) - 1) * 100)).scalars().all()
    return ok([{"id": r.id, "type": r.event_type, "ref_type": r.ref_type, "ref_id": r.ref_id,
                "details": r.details, "at": r.occurred_at.isoformat()} for r in rows],
              meta={"page": page})


@router.post("/projects/{project_id}/workflow/requeue", status_code=202)
def requeue(project_id: uuid.UUID, body: ReasonBody, session: Session = Depends(get_session),
            user: CurrentUser = Depends(_admins)) -> dict:
    get_member_project(session, user, project_id)
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="project",
                         entity_id=str(project_id), project_id=project_id, action="requeue",
                         details={"reason": body.reason})
    session.commit()
    kick_workflow(project_id)
    return ok({"requeued": True})


@router.get("/manual-reviews")
def list_reviews(status: str | None = "open", project_id: uuid.UUID | None = None,
                 session: Session = Depends(get_session), user: CurrentUser = Depends(current_user)) -> dict:
    query = select(ManualReview).order_by(ManualReview.created_at)
    if user.role != UserRole.admin:
        query = query.join(ProjectMember, ProjectMember.project_id == ManualReview.project_id).where(
            ProjectMember.user_id == user.id)
    if status:
        query = query.where(ManualReview.status == status)
    if project_id:
        query = query.where(ManualReview.project_id == project_id)
    return ok([ManualReviewOut.model_validate(r) for r in session.execute(query).scalars()])


@router.post("/manual-reviews/{review_id}/decision", status_code=202)
def decide(review_id: uuid.UUID, body: ManualDecision, session: Session = Depends(get_session),
           user: CurrentUser = Depends(_writers)) -> dict:
    review = session.get(ManualReview, review_id)
    if review is None:
        raise NotFoundError("Manual review not found")
    get_member_project(session, user, review.project_id)
    if review.status != "open":
        raise ConflictError("Manual review is already resolved")
    graph_review = review.workflow_run_id is not None
    allowed = DECISIONS_FOR_GRAPH_REVIEWS if graph_review else DECISIONS_FOR_STANDALONE
    if body.decision not in allowed:
        raise ValidationFailed(f"Decision must be one of: {', '.join(sorted(allowed))}")
    review.decision, review.decision_note = body.decision, body.note
    review.resolved_by, review.resolved_at = user.id, datetime.now(UTC).replace(tzinfo=None)
    # graph reviews wait for the workflow to apply the decision; standalone ones are done here
    review.status = "resolved" if graph_review else "applied"
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="manual_review",
                         entity_id=str(review.id), project_id=review.project_id, action="decide",
                         details={"decision": body.decision, "reason_code": review.reason_code})
    project_id = review.project_id
    session.commit()
    if graph_review:
        kick_workflow(project_id)
    return ok({"review_id": str(review.id), "status": review.status})
