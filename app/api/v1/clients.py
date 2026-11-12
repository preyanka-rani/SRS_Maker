import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, current_user, require_roles
from app.core.envelope import ok
from app.core.errors import ConflictError, NotFoundError
from app.domain.emailnorm import normalize_email
from app.database.models import Client, ClientContact, UserRole
from app.database.session import get_session
from app.schemas.identity import (
    ClientCreate,
    ClientOut,
    ContactCreate,
    ContactOut,
    ContactUpdate,
)
from app.services import audit_service

router = APIRouter(prefix="/clients", tags=["clients"])
_writers = require_roles(UserRole.admin, UserRole.analyst)


@router.post("", status_code=201)
def create_client(
    body: ClientCreate, session: Session = Depends(get_session), user: CurrentUser = Depends(_writers)
) -> dict:
    exists = session.execute(
        select(Client.id).where(func.lower(Client.organisation_name) == body.organisation_name.lower())
    ).first()
    if exists:
        raise ConflictError("A client with this name already exists")
    client = Client(**body.model_dump())
    session.add(client)
    session.flush()
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="client",
        entity_id=str(client.id), action="create",
    )
    return ok(ClientOut.model_validate(client))


@router.get("/{client_id}")
def get_client(
    client_id: uuid.UUID, session: Session = Depends(get_session), _: CurrentUser = Depends(current_user)
) -> dict:
    client = session.get(Client, client_id)
    if client is None:
        raise NotFoundError("Client not found")
    contacts = session.execute(
        select(ClientContact).where(ClientContact.client_id == client_id)
    ).scalars().all()
    data = ClientOut.model_validate(client).model_dump(mode="json")
    data["contacts"] = [ContactOut.model_validate(c).model_dump(mode="json") for c in contacts]
    return ok(data)


@router.post("/{client_id}/contacts", status_code=201)
def add_contact(
    client_id: uuid.UUID,
    body: ContactCreate,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_writers),
) -> dict:
    if session.get(Client, client_id) is None:
        raise NotFoundError("Client not found")
    contact = ClientContact(client_id=client_id, full_name=body.full_name, email=normalize_email(str(body.email)))
    session.add(contact)
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError("A contact with this email already exists for the client") from exc
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="client_contact",
        entity_id=str(contact.id), action="create",
    )
    return ok(ContactOut.model_validate(contact))


@router.patch("/{client_id}/contacts/{contact_id}")
def update_contact(
    client_id: uuid.UUID,
    contact_id: uuid.UUID,
    body: ContactUpdate,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(_writers),
) -> dict:
    contact = session.execute(
        select(ClientContact).where(
            ClientContact.id == contact_id, ClientContact.client_id == client_id
        )
    ).scalar_one_or_none()
    if contact is None:
        raise NotFoundError("Contact not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    session.flush()
    audit_service.record(
        session, actor_type="user", actor_id=str(user.id), entity_type="client_contact",
        entity_id=str(contact.id), action="update", details=body.model_dump(exclude_unset=True),
    )
    return ok(ContactOut.model_validate(contact))
