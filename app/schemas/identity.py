import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.database.models import ConsentScope, ContactRole, ProjectStatus, UserRole


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TokenRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole


class ClientCreate(BaseModel):
    organisation_name: str = Field(min_length=1, max_length=255)
    allowed_sender_domains: list[str] | None = None
    notes: str | None = None


class ClientOut(_Out):
    id: uuid.UUID
    organisation_name: str
    allowed_sender_domains: list[str] | None
    notes: str | None


class ContactCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    email: EmailStr


class ContactUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None


class ContactOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    full_name: str
    email: str
    is_active: bool


class ProjectCreate(BaseModel):
    client_id: uuid.UUID
    code: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=255)
    subtitle: str | None = None
    description: str | None = None
    auto_send_review: bool = True


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    subtitle: str | None = None
    description: str | None = None
    auto_send_review: bool | None = None


class ProjectOut(_Out):
    id: uuid.UUID
    client_id: uuid.UUID
    code: str
    title: str
    subtitle: str | None
    description: str | None
    status: ProjectStatus
    project_token: str
    auto_send_review: bool
    lock_version: int


class ProjectContactCreate(BaseModel):
    contact_id: uuid.UUID
    role: ContactRole
    can_approve: bool = False


class ProjectContactOut(_Out):
    project_id: uuid.UUID
    contact_id: uuid.UUID
    role: ContactRole
    can_approve: bool
    revoked_at: datetime | None


class ConsentCreate(BaseModel):
    scope: ConsentScope
    policy_reference: str = Field(min_length=1, max_length=255)


class ConsentRevoke(BaseModel):
    reason: str = Field(min_length=1)


class ConsentOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    scope: ConsentScope
    policy_reference: str
    granted_at: datetime
    revoked_at: datetime | None
    revocation_reason: str | None
