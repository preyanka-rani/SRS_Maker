"""Requirements, issues and their history. Project-scoped; staff edits are audited."""
import uuid

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_member_project, require_roles
from app.core.envelope import ok
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.database.models import (
    CauseType,
    IssueStatus,
    ReqStatus,
    Requirement,
    RequirementHistory,
    RequirementIssue,
    SourceEvidence,
    UserRole,
)
from app.database.session import get_session
from app.schemas.requirements import (
    IssueOut,
    IssuePatch,
    ReasonBody,
    RequirementCreate,
    RequirementPatch,
    RequirementSummary,
)
from app.services import audit_service
from app.services import requirement_service as rs

router = APIRouter(prefix="/projects/{project_id}", tags=["requirements"])
_writers = require_roles(UserRole.admin, UserRole.analyst)


def _detail(session: Session, req: Requirement) -> dict:
    snap = rs.snapshot(session, req)
    ids = [e.source_evidence_id for e in rs.evidence_at(session, req)]
    excerpts = {r.id: r for r in session.execute(
        select(SourceEvidence).where(SourceEvidence.id.in_(ids))).scalars()} if ids else {}
    snap["evidence"] = [
        {"id": str(e.source_evidence_id), "relation": e.relation.value,
         "excerpt": excerpts[e.source_evidence_id].excerpt,
         "page": excerpts[e.source_evidence_id].locator_page,
         "t0_ms": excerpts[e.source_evidence_id].locator_time_start_ms}
        for e in rs.evidence_at(session, req)]
    snap["lock_version"] = req.lock_version
    return snap


@router.get("/requirements")
def list_requirements(project_id: uuid.UUID, type: str | None = None, status: str | None = None,
                      q: str | None = None, page: int = 1, page_size: int = 100,
                      session: Session = Depends(get_session),
                      user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    page_size = max(1, min(page_size, 500))
    query = select(Requirement).where(Requirement.project_id == project_id)
    if type:
        query = query.where(Requirement.req_type == type)
    if status:
        query = query.where(Requirement.status == status)
    if q:
        query = query.where(Requirement.statement.ilike(f"%{q}%") | Requirement.title.ilike(f"%{q}%"))
    rows = session.execute(query.order_by(Requirement.code).limit(page_size)
                           .offset((max(page, 1) - 1) * page_size)).scalars().all()
    return ok([RequirementSummary.model_validate(r) for r in rows], meta={"page": page})


@router.get("/requirements/{code}")
def get_requirement(project_id: uuid.UUID, code: str, session: Session = Depends(get_session),
                    user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    return ok(_detail(session, rs.get_by_code(session, project_id, code)))


@router.get("/requirements/{code}/history")
def get_history(project_id: uuid.UUID, code: str, session: Session = Depends(get_session),
                user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    req = rs.get_by_code(session, project_id, code)
    rows = session.execute(select(RequirementHistory).where(
        RequirementHistory.requirement_id == req.id).order_by(RequirementHistory.history_seq)).scalars()
    return ok([{"history_seq": h.history_seq, "content_revision": h.content_revision,
                "change_type": h.change_type.value, "cause_type": h.cause_type.value,
                "actor_type": h.actor_type, "diff": h.diff, "snapshot": h.snapshot,
                "created_at": h.created_at.isoformat()} for h in rows])


@router.post("/requirements", status_code=201)
def create_requirement(project_id: uuid.UUID, body: RequirementCreate,
                       session: Session = Depends(get_session),
                       user: CurrentUser = Depends(_writers)) -> dict:
    get_member_project(session, user, project_id)
    draft = body.model_dump(mode="json")
    draft["details"] = body.details or {}
    draft["acceptance_criteria"] = [c.model_dump() for c in body.acceptance_criteria]
    req = rs.create_requirement(session, project_id, draft, [], cause_type=CauseType.staff_edit,
                                actor_type="user", actor_id=str(user.id))
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="requirement",
                         entity_id=str(req.id), project_id=project_id, action="create",
                         details={"code": req.code})
    return ok(_detail(session, req))


@router.patch("/requirements/{code}")
def patch_requirement(project_id: uuid.UUID, code: str, body: RequirementPatch,
                      if_match: int = Header(alias="If-Match"),
                      session: Session = Depends(get_session),
                      user: CurrentUser = Depends(_writers)) -> dict:
    get_member_project(session, user, project_id)
    req = rs.get_by_code(session, project_id, code)
    if req.status == ReqStatus.deprecated:
        raise ValidationFailed("A deprecated requirement cannot be edited")
    if req.lock_version != if_match:
        raise ConflictError("Requirement was modified by someone else",
                            {"current_lock_version": req.lock_version})
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    if "acceptance_criteria" in changes and changes["acceptance_criteria"] is not None:
        changes["acceptance_criteria"] = [c for c in changes["acceptance_criteria"]]
    if not changes:
        raise ValidationFailed("No changes supplied")
    rs.modify_requirement(session, req, changes, [], cause_type=CauseType.staff_edit,
                          cause_id=body.reason, actor_type="user", actor_id=str(user.id))
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="requirement",
                         entity_id=str(req.id), project_id=project_id, action="update",
                         details={"reason": body.reason, "fields": sorted(changes)})
    return ok(_detail(session, req))


@router.post("/requirements/{code}/deprecate")
def deprecate_requirement(project_id: uuid.UUID, code: str, body: ReasonBody,
                          session: Session = Depends(get_session),
                          user: CurrentUser = Depends(_writers)) -> dict:
    get_member_project(session, user, project_id)
    req = rs.get_by_code(session, project_id, code)
    if req.status == ReqStatus.deprecated:
        raise ConflictError("Requirement is already deprecated")
    rs.deprecate(session, req, [], cause_type=CauseType.staff_edit, cause_id=body.reason,
                 actor_type="user", actor_id=str(user.id))
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="requirement",
                         entity_id=str(req.id), project_id=project_id, action="deprecate",
                         details={"reason": body.reason})
    return ok(_detail(session, req))


@router.get("/issues")
def list_issues(project_id: uuid.UUID, type: str | None = None, status: str | None = None,
                blocking: bool | None = None, session: Session = Depends(get_session),
                user: CurrentUser = Depends(current_user)) -> dict:
    get_member_project(session, user, project_id)
    query = select(RequirementIssue).where(RequirementIssue.project_id == project_id)
    if type:
        query = query.where(RequirementIssue.issue_type == type)
    if status:
        query = query.where(RequirementIssue.status == status)
    if blocking is not None:
        query = query.where(RequirementIssue.blocking.is_(blocking))
    return ok([IssueOut.model_validate(i) for i in session.execute(
        query.order_by(RequirementIssue.created_at)).scalars()])


@router.patch("/issues/{issue_id}")
def patch_issue(project_id: uuid.UUID, issue_id: uuid.UUID, body: IssuePatch,
                session: Session = Depends(get_session),
                user: CurrentUser = Depends(_writers)) -> dict:
    get_member_project(session, user, project_id)
    issue = session.execute(select(RequirementIssue).where(
        RequirementIssue.project_id == project_id, RequirementIssue.id == issue_id)).scalar_one_or_none()
    if issue is None:
        raise NotFoundError("Issue not found")
    if body.status in ("resolved", "dismissed") and not body.resolution_text:
        raise ValidationFailed("resolution_text is required when resolving or dismissing an issue")
    issue.status = IssueStatus(body.status)
    issue.resolution_text = body.resolution_text or issue.resolution_text
    audit_service.record(session, actor_type="user", actor_id=str(user.id), entity_type="issue",
                         entity_id=str(issue.id), project_id=project_id, action="update",
                         details={"status": body.status})
    return ok(IssueOut.model_validate(issue))
