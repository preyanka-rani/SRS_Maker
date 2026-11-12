"""Clients, contacts, projects, consent, staff users, audit (Phase 1).

Project-scoped integrity: project_contacts references the project AND the contact through
composite FKs that both carry client_id, so a contact can only be authorized on a project
of its own client.
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedMixin, IdMixin, TimestampMixin

JsonType = JSON().with_variant(JSONB(), "postgresql")


class UserRole(str, enum.Enum):
    admin = "admin"
    analyst = "analyst"
    viewer = "viewer"


class ContactRole(str, enum.Enum):
    reviewer = "reviewer"
    approver = "approver"
    cc = "cc"


class ProjectStatus(str, enum.Enum):
    draft = "draft"
    extracting = "extracting"
    in_review = "in_review"
    awaiting_client = "awaiting_client"
    revising = "revising"
    manual_review = "manual_review"
    approved = "approved"
    finalized = "finalized"
    cancelled = "cancelled"


class ConsentScope(str, enum.Enum):
    text_processing = "text_processing"
    audio_processing = "audio_processing"


class AppUser(Base, IdMixin, TimestampMixin):
    __tablename__ = "app_users"
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))


class Client(Base, IdMixin, TimestampMixin):
    __tablename__ = "clients"
    organisation_name: Mapped[str] = mapped_column(String(255), nullable=False)
    allowed_sender_domains: Mapped[list[str] | None] = mapped_column(ARRAY(String(320)))
    notes: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("uq_clients_org_ci", func.lower(organisation_name), unique=True),)


class ClientContact(Base, IdMixin, TimestampMixin):
    __tablename__ = "client_contacts"
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    __table_args__ = (
        UniqueConstraint("client_id", "email", name="uq_client_contacts_client_email"),
        UniqueConstraint("client_id", "id", name="uq_client_contacts_client_id_id"),
    )


class Project(Base, IdMixin, TimestampMixin):
    __tablename__ = "projects"
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        Enum(ProjectStatus, name="project_status"), default=ProjectStatus.draft, nullable=False
    )
    project_token: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    # Composite deferrable FK (id, current_srs_version_id) -> srs_versions(project_id, id).
    current_srs_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    template_version: Mapped[int | None] = mapped_column(Integer)
    auto_send_review: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=text("true")
    )
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    lock_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    __table_args__ = (
        UniqueConstraint("id", "client_id", name="uq_projects_id_client_id"),
        ForeignKeyConstraint(
            ["id", "current_srs_version_id"],
            ["srs_versions.project_id", "srs_versions.id"],
            name="fk_projects_current_version",
            use_alter=True, deferrable=True, initially="DEFERRED",
        ),
        Index("ix_projects_client_id", "client_id"),
        Index("ix_projects_status", "status"),
    )


class ProjectContact(Base, CreatedMixin):
    __tablename__ = "project_contacts"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    role: Mapped[ContactRole] = mapped_column(
        Enum(ContactRole, name="contact_role"), primary_key=True
    )
    client_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    can_approve: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "client_id"],
            ["projects.id", "projects.client_id"],
            name="fk_project_contacts_project_client",
        ),
        ForeignKeyConstraint(
            ["client_id", "contact_id"],
            ["client_contacts.client_id", "client_contacts.id"],
            name="fk_project_contacts_client_contact",
        ),
    )


class ProjectLlmConsent(Base, IdMixin):
    __tablename__ = "project_llm_consents"
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    scope: Mapped[ConsentScope] = mapped_column(
        Enum(ConsentScope, name="consent_scope"), nullable=False
    )
    policy_reference: Mapped[str] = mapped_column(String(255), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_users.id"))
    granted_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column()
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        Index(
            "uq_consent_active",
            "project_id",
            "scope",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class ProjectMember(Base):
    __tablename__ = "project_members"
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_users.id", ondelete="RESTRICT"), primary_key=True
    )


class AuditLog(Base):
    """Append-only, hash-chained audit trail (trigger added in the migration)."""

    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now(), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict | None] = mapped_column(JsonType)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
