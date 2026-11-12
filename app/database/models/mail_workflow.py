"""Email, feedback, approvals, workflow runs, event log, manual reviews, LLM ledger."""
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
    Sequence,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, CreatedMixin, IdMixin, TimestampMixin
from app.database.models.identity import JsonType

def _project_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT")


class MatchMethod(str, enum.Enum):
    headers = "headers"
    subject_token_sender = "subject_token_sender"
    manual = "manual"
    unmatched = "unmatched"


class MailStatus(str, enum.Enum):
    received = "received"
    matched = "matched"
    unmatched = "unmatched"
    queued = "queued"
    processing = "processing"
    processed = "processed"
    ignored = "ignored"
    failed = "failed"


class SenderAuth(str, enum.Enum):
    pass_ = "pass"
    fail = "fail"
    unknown = "unknown"


class OutboxStatus(str, enum.Enum):
    pending = "pending"
    sending = "sending"
    sent = "sent"
    failed = "failed"
    bounced = "bounced"
    dead = "dead"
    cancelled = "cancelled"


class FeedbackCategory(str, enum.Enum):
    NEW_REQUIREMENT = "NEW_REQUIREMENT"
    MODIFY_REQUIREMENT = "MODIFY_REQUIREMENT"
    REMOVE_REQUIREMENT = "REMOVE_REQUIREMENT"
    CLARIFICATION_REQUEST = "CLARIFICATION_REQUEST"
    REQUIREMENT_CONFIRMATION = "REQUIREMENT_CONFIRMATION"
    APPROVAL = "APPROVAL"
    REJECTION = "REJECTION"
    GENERAL_QUESTION = "GENERAL_QUESTION"
    UNKNOWN = "UNKNOWN"


class EmailMailboxState(Base):
    __tablename__ = "email_mailbox_state"
    mailbox: Mapped[str] = mapped_column(String(255), primary_key=True)
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    last_polled_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)


class EmailThread(Base, IdMixin, CreatedMixin):
    __tablename__ = "email_threads"
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    root_message_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    subject_normalized: Mapped[str | None] = mapped_column(String(512))
    __table_args__ = (
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
    )


class EmailMessage(Base, IdMixin, CreatedMixin):
    __tablename__ = "email_messages"
    inbound_seq: Mapped[int] = mapped_column(
        BigInteger, Sequence("email_inbound_seq"), nullable=False,
        server_default=Sequence("email_inbound_seq").next_value(),
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    direction: Mapped[str] = mapped_column(String(8), default="inbound")
    message_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(512))
    references: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    from_address: Mapped[str | None] = mapped_column(String(320))
    from_count: Mapped[int] = mapped_column(SmallInteger, default=1)
    sender_address: Mapped[str | None] = mapped_column(String(320))
    reply_to_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(String(320)))
    to_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(String(320)))
    cc_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(String(320)))
    subject: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    new_content_text: Mapped[str | None] = mapped_column(Text)
    raw_path: Mapped[str | None] = mapped_column(String(1024))
    raw_sha256: Mapped[str | None] = mapped_column(String(64))
    imap_uid: Mapped[int | None] = mapped_column(BigInteger)
    imap_uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime | None] = mapped_column()
    auth_results_trusted: Mapped[dict | None] = mapped_column(JsonType)
    auth_results_untrusted_count: Mapped[int] = mapped_column(Integer, default=0)
    sender_auth_verdict: Mapped[SenderAuth] = mapped_column(
        Enum(SenderAuth, name="sender_auth"), default=SenderAuth.unknown
    )
    is_auto_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    is_dsn: Mapped[bool] = mapped_column(Boolean, default=False)
    match_method: Mapped[MatchMethod] = mapped_column(
        Enum(MatchMethod, name="match_method"), default=MatchMethod.unmatched
    )
    match_failure_reason: Mapped[str | None] = mapped_column(String(128))
    processing_status: Mapped[MailStatus] = mapped_column(
        Enum(MailStatus, name="mail_status"), default=MailStatus.received
    )
    purged_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        UniqueConstraint("project_id", "id", name="uq_email_messages_project_id_id"),
        Index("ix_email_messages_queue", "project_id", "processing_status", "inbound_seq"),
    )


class EmailOutbox(Base, IdMixin, TimestampMixin):
    __tablename__ = "email_outbox"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    srs_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    to_addresses: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    cc_addresses: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list | None] = mapped_column(JsonType)
    message_id: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(512))
    references: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status"), default=OutboxStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(server_default=func.now())
    sending_started_at: Mapped[datetime | None] = mapped_column()
    last_smtp_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column()
    bounced_at: Mapped[datetime | None] = mapped_column()
    bounce_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        _project_fk(),
        Index("ix_email_outbox_due", "status", "next_attempt_at"),
    )


class FeedbackRecord(Base, IdMixin, CreatedMixin):
    __tablename__ = "feedback_records"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    email_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    referenced_srs_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    category: Mapped[FeedbackCategory] = mapped_column(
        Enum(FeedbackCategory, name="feedback_category")
    )
    secondary_categories: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    has_unprocessed_attachments: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    llm_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    llm_output: Mapped[dict | None] = mapped_column(JsonType)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    processed_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "email_message_id"],
            ["email_messages.project_id", "email_messages.id"],
            name="fk_feedback_email",
        ),
        UniqueConstraint("project_id", "id", name="uq_feedback_project_id_id"),
    )


class FeedbackChangeItem(Base, IdMixin):
    __tablename__ = "feedback_change_items"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    feedback_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    target_requirement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    proposed_title: Mapped[str | None] = mapped_column(String(512))
    proposed_statement: Mapped[str | None] = mapped_column(Text)
    req_type: Mapped[str | None] = mapped_column(String(32))
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    applied_history_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), default="proposed")
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "feedback_record_id"],
            ["feedback_records.project_id", "feedback_records.id"],
            name="fk_change_items_feedback",
        ),
        ForeignKeyConstraint(
            ["project_id", "target_requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_change_items_requirement",
        ),
    )


class Approval(Base, IdMixin, CreatedMixin):
    """Append-only. Stores the approved snapshot's hashes."""

    __tablename__ = "approvals"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    srs_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    email_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True)
    contact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decision: Mapped[str] = mapped_column(String(16), default="approve")
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    checks: Mapped[dict] = mapped_column(JsonType, nullable=False)
    evidence_excerpt: Mapped[str | None] = mapped_column(Text)
    approved_substantive_hash: Mapped[str | None] = mapped_column(String(64))
    approved_content_hash: Mapped[str | None] = mapped_column(String(64))
    approved_docx_sha256: Mapped[str | None] = mapped_column(String(64))
    approved_pdf_sha256: Mapped[str | None] = mapped_column(String(64))
    final_srs_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_by: Mapped[str] = mapped_column(String(16), default="system")
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "srs_version_id"],
            ["srs_versions.project_id", "srs_versions.id"],
            name="fk_approvals_version",
        ),
        ForeignKeyConstraint(
            ["project_id", "email_message_id"],
            ["email_messages.project_id", "email_messages.id"],
            name="fk_approvals_email",
        ),
        Index("uq_approvals_one_accepted", "project_id", unique=True,
              postgresql_where=text("outcome = 'accepted'")),
    )


class WorkflowRun(Base, IdMixin):
    __tablename__ = "workflow_runs"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(80), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_ref: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="running")
    current_node: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "id", name="uq_workflow_runs_project_id_id"),
    )


class WorkflowEventLog(Base):
    """Audit/history only; never drives workers. Append-only."""

    __tablename__ = "workflow_event_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JsonType)
    __table_args__ = (_project_fk(),)


class ManualReview(Base, IdMixin, CreatedMixin):
    __tablename__ = "manual_reviews"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    related_entity_type: Mapped[str | None] = mapped_column(String(32))
    related_entity_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="open")
    decision: Mapped[str | None] = mapped_column(String(32))
    decision_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column()
    fingerprint: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    __table_args__ = (_project_fk(),)


class LlmCall(Base, IdMixin, CreatedMixin):
    """Ledger of external AI calls. No raw payloads."""

    __tablename__ = "llm_calls"
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_id: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(16))
    output_schema_version: Mapped[str | None] = mapped_column(String(32))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    output_sha256: Mapped[str | None] = mapped_column(String(64))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    audio_seconds: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    injection_flags: Mapped[list[str] | None] = mapped_column(ARRAY(String))
