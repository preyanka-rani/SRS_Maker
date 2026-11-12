import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, get_member_project, require_roles
from app.core.envelope import ok
from app.core.errors import ConflictError, NotFoundError, ValidationFailed
from app.database.models import (
    Client,
    ConsentScope,
    ClientContact,
    ContactRole,
    Project,
    ProjectContact,
    ProjectLlmConsent,
    ProjectMember,
    UserRole,
)
from app.database.session import get_session
from app.domain.project_token import generate_project_token
from app.schemas.identity import (
    ConsentCreate,
    ConsentOut,
    ConsentRevoke,
    ProjectContactCreate,
    ProjectContactOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
)
from app.services import audit_service
from app.services.ingestion_service import sources_awaiting_consent
from app.workers.tasks import dispatch_source, kick_workflow

router = APIRouter(prefix="/projects", tags=["projects"])
_writers = require_roles(UserRole.admin, UserRole.analyst)
_admins = require_roles(UserRole.admin)
_TOKEN_ATTEMPTS = 5


@router.post("", status_code=201)
def create_project(
    body: ProjectCreate, session: Session = Depends(get_session), user: CurrentUser = Depends(_writers)
) -> dict:
    if session.get(Client, body.client_id) is None:
        raise NotFoundError("Client not found")
    if session.execute(select(Project.id).where(Project.code == body.code)).first():
        raise ConflictError("Project code already exists")
    for _ in range(_TOKEN_ATTEMPTS):
        token = generate_project_token()
        if not session.execute(select(Project.id).where(Project.project_token == token)).first():
            break
    else:  # pragma: no cover - astronomically unlikely
        raise ConflictError("Could not allocate a unique project token")
    project = Project(**body.model_dump(), project_token=token)
    session.add(project)
    session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=user.id))
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="project",
        entity_id=str(project.id), project_id=project.id, action="create",
    )
    return ok(ProjectOut.model_validate(project))


@router.get("")
def list_projects(
    status: str | None = None,
    client_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 50,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(current_user),
) -> dict:
    page_size = max(1, min(page_size, 200))
    page = max(1, page)
    query = select(Project).order_by(Project.created_at.desc())
    if user.role != UserRole.admin:
        query = query.join(ProjectMember, ProjectMember.project_id == Project.id).where(
            ProjectMember.user_id == user.id
        )
    if status:
        query = query.where(Project.status == status)
    if client_id:
        query = query.where(Project.client_id == client_id)
    rows = session.execute(query.limit(page_size).offset((page - 1) * page_size)).scalars().all()
    return ok(
        [ProjectOut.model_validate(p) for p in rows], meta={"page": page, "page_size": page_size}
    )


@router.get("/{project_id}")
def get_project(
    project_id: uuid.UUID, session: Session = Depends(get_session), user: CurrentUser = Depends(current_user)
) -> dict:
    return ok(ProjectOut.model_validate(get_member_project(session, user, project_id)))


@router.patch("/{project_id}")
def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    if_match: int = Header(alias="If-Match"),
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_writers),
) -> dict:
    project = get_member_project(session, user, project_id)
    if project.lock_version != if_match:
        raise ConflictError(
            "Project was modified by someone else", {"current_lock_version": project.lock_version}
        )
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(project, field, value)
    project.lock_version += 1
    session.flush()
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="project",
        entity_id=str(project.id), project_id=project.id, action="update", details=changes,
    )
    return ok(ProjectOut.model_validate(project))


@router.post("/{project_id}/contacts", status_code=201)
def authorize_contact(
    project_id: uuid.UUID,
    body: ProjectContactCreate,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_writers),
) -> dict:
    project = get_member_project(session, user, project_id)
    contact = session.get(ClientContact, body.contact_id)
    if contact is None:
        raise NotFoundError("Contact not found")
    if contact.client_id != project.client_id:
        raise ValidationFailed("Contact belongs to a different client than this project")
    if body.can_approve and body.role != ContactRole.approver:
        raise ValidationFailed("can_approve is only valid for the approver role")
    row = ProjectContact(
        project_id=project.id, contact_id=contact.id, client_id=project.client_id,
        role=body.role, can_approve=body.can_approve, granted_by=user.id,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError("Contact already holds this role on the project") from exc
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="project_contact",
        entity_id=str(contact.id), project_id=project.id, action="authorize",
        details={"role": body.role.value, "can_approve": body.can_approve},
    )
    return ok(ProjectContactOut.model_validate(row))


@router.delete("/{project_id}/contacts/{contact_id}", status_code=204)
def revoke_contact(
    project_id: uuid.UUID,
    contact_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_writers),
) -> None:
    get_member_project(session, user, project_id)
    rows = session.execute(
        select(ProjectContact).where(
            ProjectContact.project_id == project_id,
            ProjectContact.contact_id == contact_id,
            ProjectContact.revoked_at.is_(None),
        )
    ).scalars().all()
    if not rows:
        raise NotFoundError("Active authorization not found")
    now = datetime.now(UTC)
    for row in rows:
        row.revoked_at = now
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="project_contact",
        entity_id=str(contact_id), project_id=project_id, action="revoke",
    )


@router.post("/{project_id}/consents", status_code=201)
def grant_consent(
    project_id: uuid.UUID,
    body: ConsentCreate,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_admins),
) -> dict:
    get_member_project(session, user, project_id)
    consent = ProjectLlmConsent(
        project_id=project_id, scope=body.scope,
        policy_reference=body.policy_reference, granted_by=user.id,
    )
    session.add(consent)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError("An active consent for this scope already exists") from exc
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="consent",
        entity_id=str(consent.id), project_id=project_id, action="grant",
        details={"scope": body.scope.value, "policy_reference": body.policy_reference},
    )
    out = ConsentOut.model_validate(consent)
    session.commit()  # workers must see the consent
    if body.scope == ConsentScope.audio_processing:
        for held in sources_awaiting_consent(session, project_id):
            dispatch_source(held)
    kick_workflow(project_id)
    return ok(out)


@router.delete("/{project_id}/consents/{scope}")
def revoke_consent(
    project_id: uuid.UUID,
    scope: str,
    body: ConsentRevoke,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_admins),
) -> dict:
    get_member_project(session, user, project_id)
    consent = session.execute(
        select(ProjectLlmConsent).where(
            ProjectLlmConsent.project_id == project_id,
            ProjectLlmConsent.scope == scope,
            ProjectLlmConsent.revoked_at.is_(None),
        )
    ).scalar_one_or_none()
    if consent is None:
        raise NotFoundError("No active consent for this scope")
    consent.revoked_at = datetime.now(UTC)
    consent.revoked_by = user.id
    consent.revocation_reason = body.reason
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="consent",
        entity_id=str(consent.id), project_id=project_id, action="revoke",
        details={"scope": scope, "reason": body.reason},
    )
    return ok(ConsentOut.model_validate(consent))


@router.get("/{project_id}/consents")
def list_consents(
    project_id: uuid.UUID, session: Session = Depends(get_session), user: CurrentUser = Depends(current_user)
) -> dict:
    get_member_project(session, user, project_id)
    rows = session.execute(
        select(ProjectLlmConsent)
        .where(ProjectLlmConsent.project_id == project_id)
        .order_by(ProjectLlmConsent.granted_at)
    ).scalars().all()
    return ok([ConsentOut.model_validate(r) for r in rows])
