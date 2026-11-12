"""Templates and immutable SRS versions (artifact columns frozen by trigger after generating)."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedMixin, IdMixin
from app.database.models.identity import JsonType


class VersionKind(str, enum.Enum):
    draft = "draft"
    final = "final"


class VersionStatus(str, enum.Enum):
    generating = "generating"
    failed = "failed"
    qa_failed = "qa_failed"
    ready = "ready"
    sent = "sent"
    superseded = "superseded"
    approved = "approved"
    final = "final"


class SrsTemplate(Base, IdMixin, CreatedMixin):
    __tablename__ = "srs_templates"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    repo_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_path: Mapped[str | None] = mapped_column(String(512))
    manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    __table_args__ = (UniqueConstraint("name", "template_version", name="uq_srs_templates"),)


class SrsVersion(Base, IdMixin, CreatedMixin):
    __tablename__ = "srs_versions"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    version_label: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[VersionKind] = mapped_column(Enum(VersionKind, name="version_kind"))
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status"), default=VersionStatus.generating
    )
    base_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    generation_attempt: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    template_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    template_version: Mapped[int | None] = mapped_column(Integer)
    prompt_versions: Mapped[dict | None] = mapped_column(JsonType)
    renderer_version: Mapped[str | None] = mapped_column(String(32))
    hash_scheme_version: Mapped[int] = mapped_column(Integer, default=1)
    alembic_revision: Mapped[str | None] = mapped_column(String(64))
    created_by_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ready_at: Mapped[datetime | None] = mapped_column()
    sent_at: Mapped[datetime | None] = mapped_column()
    approved_at: Mapped[datetime | None] = mapped_column()
    superseded_at: Mapped[datetime | None] = mapped_column()
    finalized_at: Mapped[datetime | None] = mapped_column()
    # ---- artifact columns: writable only while status = 'generating' ----
    input_fingerprint: Mapped[str | None] = mapped_column(String(64))
    requirement_set_hash: Mapped[str | None] = mapped_column(String(64))
    substantive_hash: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    docx_path: Mapped[str | None] = mapped_column(String(1024))
    docx_sha256: Mapped[str | None] = mapped_column(String(64))
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    qa_report: Mapped[dict | None] = mapped_column(JsonType)
    change_summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "base_version_id"],
            ["srs_versions.project_id", "srs_versions.id"],
            name="fk_srs_versions_base",
        ),
        UniqueConstraint("project_id", "id", name="uq_srs_versions_project_id_id"),
        UniqueConstraint("project_id", "seq", name="uq_srs_versions_seq"),
        Index(
            "uq_srs_versions_label_live", "project_id", "version_label", unique=True,
            postgresql_where=text("status NOT IN ('failed','qa_failed')"),
        ),
        Index("uq_srs_versions_one_generating", "project_id", unique=True,
              postgresql_where=text("status = 'generating'")),
        Index("uq_srs_versions_one_final", "project_id", unique=True,
              postgresql_where=text("kind = 'final' AND status = 'final'")),
    )


class SrsVersionRequirement(Base):
    __tablename__ = "srs_version_requirements"
    srs_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status_at_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "srs_version_id"],
            ["srs_versions.project_id", "srs_versions.id"],
            name="fk_svr_version",
        ),
        ForeignKeyConstraint(
            ["project_id", "requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_svr_requirement",
        ),
    )


class SrsSection(Base, IdMixin):
    __tablename__ = "srs_sections"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    srs_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    section_key: Mapped[str] = mapped_column(String(64), nullable=False)
    rendered: Mapped[bool] = mapped_column(default=True)
    content: Mapped[dict | list | None] = mapped_column(JsonType)
    generation_method: Mapped[str] = mapped_column(String(4), default="D")
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "srs_version_id"],
            ["srs_versions.project_id", "srs_versions.id"],
            name="fk_srs_sections_version",
        ),
        UniqueConstraint("srs_version_id", "section_key", name="uq_srs_sections_key"),
    )
