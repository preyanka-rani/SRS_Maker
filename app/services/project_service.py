"""Project creation helpers shared by the API and by mailbox intake."""
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.database.models import (
    Client,
    ClientContact,
    ConsentScope,
    ContactRole,
    Project,
    ProjectContact,
    ProjectLlmConsent,
)
from app.domain.project_token import generate_project_token
from app.services import audit_service

_PREFIX = re.compile(r"^\s*((re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


def clean_subject(subject: str) -> str:
    cleaned = re.sub(r"\[PRJ-[A-Z0-9]{6}\]", "", subject or "")
    cleaned = _PREFIX.sub("", cleaned.strip()).strip()
    return cleaned[:120] or "New software project"


def _unique_token(session: Session) -> str:
    for _ in range(10):
        token = generate_project_token()
        if not session.execute(select(Project.id).where(Project.project_token == token)).first():
            return token
    raise RuntimeError("could not allocate a project token")  # pragma: no cover


def find_or_create_contact(session: Session, email: str, display_name: str | None) -> ClientContact:
    contact = session.execute(select(ClientContact).where(
        ClientContact.email == email, ClientContact.is_active.is_(True))).scalars().first()
    if contact:
        return contact
    name = display_name or email.split("@")[0]
    org = f"{name} ({email})"
    client = session.execute(select(Client).where(
        func.lower(Client.organisation_name) == org.lower())).scalar_one_or_none()
    if client is None:
        client = Client(organisation_name=org)
        session.add(client)
        session.flush()
    contact = ClientContact(client_id=client.id, full_name=name, email=email)
    session.add(contact)
    session.flush()
    return contact


def create_project_for_contact(session: Session, settings: Settings, contact: ClientContact,
                               subject: str, *, grant_consent: bool = True) -> Project:
    token = _unique_token(session)
    project = Project(client_id=contact.client_id, code=f"EML-{token[4:]}",
                      title=clean_subject(subject), project_token=token,
                      description="Created automatically from an inbound email request.")
    session.add(project)
    session.flush()
    session.add(ProjectContact(project_id=project.id, contact_id=contact.id,
                               client_id=contact.client_id, role=ContactRole.approver,
                               can_approve=True))
    if grant_consent and settings.auto_consent_policy_reference:
        for scope in (ConsentScope.text_processing, ConsentScope.audio_processing):
            session.add(ProjectLlmConsent(project_id=project.id, scope=scope,
                                          policy_reference=settings.auto_consent_policy_reference))
    session.flush()
    audit_service.record(session, actor_type="system", actor_id="mailbox-intake",
                         entity_type="project", entity_id=str(project.id), project_id=project.id,
                         action="create_from_email", details={"contact": contact.email})
    return project


def contacts_for_project(session: Session, project_id: uuid.UUID
                         ) -> list[tuple[ClientContact, ProjectContact]]:
    rows = session.execute(
        select(ClientContact, ProjectContact)
        .join(ProjectContact, ProjectContact.contact_id == ClientContact.id)
        .where(ProjectContact.project_id == project_id, ProjectContact.revoked_at.is_(None),
               ClientContact.is_active.is_(True))).all()
    return [(c, pc) for c, pc in rows]
