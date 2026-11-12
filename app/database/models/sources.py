"""Input sources (uploads, email bodies/attachments, audio) and verbatim evidence."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Enum,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedMixin, IdMixin


class SourceType(str, enum.Enum):
    upload = "upload"
    email_body = "email_body"
    email_attachment = "email_attachment"


class ProcessingClass(str, enum.Enum):
    text_supported = "text_supported"
    audio = "audio"
    video = "video"
    image = "image"
    office_other = "office_other"
    archive = "archive"
    forwarded_email = "forwarded_email"
    inline_decoration = "inline_decoration"
    dangerous = "dangerous"
    unknown = "unknown"


class ExtractionStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    transcribing = "transcribing"
    succeeded = "succeeded"
    failed = "failed"
    needs_ocr = "needs_ocr"
    awaiting_consent = "awaiting_consent"
    deferred_unsupported = "deferred_unsupported"
    rejected_dangerous = "rejected_dangerous"
    rejected_policy = "rejected_policy"


class TranscriptQuality(str, enum.Enum):
    ok = "ok"
    low_confidence = "low_confidence"
    no_speech = "no_speech"


class InputSource(Base, IdMixin, CreatedMixin):
    __tablename__ = "input_sources"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    parent_input_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_type: Mapped[SourceType] = mapped_column(Enum(SourceType, name="source_type"))
    email_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    original_filename: Mapped[str | None] = mapped_column(String(512))
    content_id: Mapped[str | None] = mapped_column(String(255))
    is_inline: Mapped[bool] = mapped_column(Boolean, default=False)
    mime_declared: Mapped[str | None] = mapped_column(String(255))
    mime_detected: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    processing_class: Mapped[ProcessingClass] = mapped_column(
        Enum(ProcessingClass, name="processing_class"), default=ProcessingClass.unknown
    )
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    extracted_text_path: Mapped[str | None] = mapped_column(String(1024))
    page_count: Mapped[int | None] = mapped_column(Integer)
    char_count: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    transcript_path: Mapped[str | None] = mapped_column(String(1024))
    transcript_segments_path: Mapped[str | None] = mapped_column(String(1024))
    transcription_model: Mapped[str | None] = mapped_column(String(128))
    transcription_language: Mapped[str | None] = mapped_column(String(16))
    transcript_quality: Mapped[TranscriptQuality | None] = mapped_column(
        Enum(TranscriptQuality, name="transcript_quality")
    )
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        Enum(ExtractionStatus, name="extraction_status"), default=ExtractionStatus.pending
    )
    extraction_error: Mapped[str | None] = mapped_column(Text)
    consumed_by_workflow_at: Mapped[datetime | None] = mapped_column()
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    purged_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "parent_input_source_id"],
            ["input_sources.project_id", "input_sources.id"],
            name="fk_input_sources_parent",
        ),
        UniqueConstraint("project_id", "id", name="uq_input_sources_project_id_id"),
        UniqueConstraint("project_id", "sha256", "source_type", name="uq_input_sources_dedup"),
        Index("ix_input_sources_status", "project_id", "extraction_status"),
        Index("ix_input_sources_consumed", "project_id", "consumed_by_workflow_at"),
    )


class SourceEvidence(Base, IdMixin):
    __tablename__ = "source_evidence"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    input_source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    locator_page: Mapped[int | None] = mapped_column(Integer)
    locator_paragraph: Mapped[int | None] = mapped_column(Integer)
    locator_time_start_ms: Mapped[int | None] = mapped_column(BigInteger)
    locator_time_end_ms: Mapped[int | None] = mapped_column(BigInteger)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    asr_avg_logprob: Mapped[float | None] = mapped_column(Float)
    asr_no_speech_prob: Mapped[float | None] = mapped_column(Float)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "input_source_id"],
            ["input_sources.project_id", "input_sources.id"],
            name="fk_source_evidence_source",
        ),
        UniqueConstraint("input_source_id", "seq", name="uq_source_evidence_seq"),
        UniqueConstraint("project_id", "id", name="uq_source_evidence_project_id_id"),
    )

