"""Requirements with stable IDs, two revision counters, revision-aware evidence, history, issues."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Enum,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
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


class ReqType(str, enum.Enum):
    FR = "FR"
    NFR = "NFR"
    BR = "BR"
    SEC = "SEC"
    BUS_RULE = "BUS_RULE"
    INT = "INT"
    CON = "CON"
    ASM = "ASM"
    DEP = "DEP"
    REPORT = "REPORT"
    UI_SCREEN = "UI_SCREEN"
    SCOPE_EXCLUSION = "SCOPE_EXCLUSION"
    FUTURE_SCOPE = "FUTURE_SCOPE"
    TECH = "TECH"


class ReqStatus(str, enum.Enum):
    proposed = "proposed"
    confirmed = "confirmed"
    modified = "modified"
    deprecated = "deprecated"
    needs_clarification = "needs_clarification"


class Priority(str, enum.Enum):
    must = "must"
    should = "should"
    could = "could"
    wont = "wont"


class EvidenceRelation(str, enum.Enum):
    origin = "origin"
    modifies = "modifies"
    confirms = "confirms"
    removes = "removes"
    clarifies = "clarifies"


class ChangeType(str, enum.Enum):
    created = "created"
    modified = "modified"
    status_changed = "status_changed"
    deprecated = "deprecated"
    restored = "restored"


class CauseType(str, enum.Enum):
    input_source = "input_source"
    feedback = "feedback"
    staff_edit = "staff_edit"
    approval = "approval"
    manual_review = "manual_review"


class IssueType(str, enum.Enum):
    missing_info = "missing_info"
    ambiguity = "ambiguity"
    conflict = "conflict"
    duplicate = "duplicate"
    unsupported_assumption = "unsupported_assumption"
    missing_business_rule = "missing_business_rule"
    missing_integration = "missing_integration"
    missing_workflow = "missing_workflow"
    missing_acceptance_criteria = "missing_acceptance_criteria"
    migration = "migration"
    design_pending = "design_pending"
    unprocessed_attachment = "unprocessed_attachment"
    low_confidence_transcript = "low_confidence_transcript"
    unsupported_language = "unsupported_language"
    injection_suspected = "injection_suspected"


class IssueStatus(str, enum.Enum):
    open = "open"
    clarification_requested = "clarification_requested"
    resolved = "resolved"
    dismissed = "dismissed"


class CandidateDisposition(str, enum.Enum):
    pending = "pending"
    merged_new = "merged_new"
    merged_existing = "merged_existing"
    rejected_duplicate = "rejected_duplicate"
    rejected_invalid = "rejected_invalid"


def _project_fk() -> ForeignKeyConstraint:
    return ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT")


class ProjectModule(Base, IdMixin, TimestampMixin):
    __tablename__ = "project_modules"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "id", name="uq_project_modules_project_id_id"),
        UniqueConstraint("project_id", "code", name="uq_project_modules_code"),
    )


class RequirementIdCounter(Base):
    __tablename__ = "requirement_id_counters"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    prefix: Mapped[str] = mapped_column(String(32), primary_key=True)
    next_value: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (_project_fk(),)


class Requirement(Base, IdMixin, TimestampMixin):
    __tablename__ = "requirements"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    req_type: Mapped[ReqType] = mapped_column(Enum(ReqType, name="req_type"), nullable=False)
    category: Mapped[str | None] = mapped_column(String(32))
    module_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    source_language: Mapped[str | None] = mapped_column(String(8))
    priority: Mapped[Priority | None] = mapped_column(Enum(Priority, name="priority"))
    status: Mapped[ReqStatus] = mapped_column(
        Enum(ReqStatus, name="req_status"), default=ReqStatus.proposed, nullable=False
    )
    content_revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    history_seq: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    lock_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    normalized_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ai_confidence: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        _project_fk(),
        ForeignKeyConstraint(
            ["project_id", "module_id"],
            ["project_modules.project_id", "project_modules.id"],
            name="fk_requirements_module",
        ),
        UniqueConstraint("project_id", "id", name="uq_requirements_project_id_id"),
        UniqueConstraint("project_id", "code", name="uq_requirements_code"),
        Index("ix_requirements_type_status", "project_id", "req_type", "status"),
        Index("ix_requirements_hash", "project_id", "normalized_hash"),
    )


class FunctionalDetails(Base):
    __tablename__ = "functional_details"
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    purpose: Mapped[str | None] = mapped_column(Text)
    actors: Mapped[str | None] = mapped_column(Text)
    trigger: Mapped[str | None] = mapped_column(Text)
    preconditions: Mapped[str | None] = mapped_column(Text)
    main_flow: Mapped[list | None] = mapped_column(JsonType)
    alternative_flows: Mapped[list | None] = mapped_column(JsonType)
    exception_flows: Mapped[list | None] = mapped_column(JsonType)
    business_rule_refs: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    authorization: Mapped[str | None] = mapped_column(Text)
    state_transition: Mapped[str | None] = mapped_column(Text)
    notifications_text: Mapped[str | None] = mapped_column(Text)
    audit_text: Mapped[str | None] = mapped_column(Text)
    related_ui: Mapped[str | None] = mapped_column(Text)
    related_data: Mapped[str | None] = mapped_column(Text)
    source_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_functional_details_requirement",
        ),
    )


class AcceptanceCriterion(Base, IdMixin):
    __tablename__ = "acceptance_criteria"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    scenario_title: Mapped[str | None] = mapped_column(String(512))
    given_text: Mapped[str] = mapped_column(Text, nullable=False)
    when_text: Mapped[str] = mapped_column(Text, nullable=False)
    then_text: Mapped[str] = mapped_column(Text, nullable=False)
    test_case_code: Mapped[str | None] = mapped_column(String(64))
    introduced_in_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    retired_in_revision: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_acceptance_criteria_requirement",
        ),
        UniqueConstraint("requirement_id", "seq", "introduced_in_revision", name="uq_ac_seq_rev"),
    )


class RequirementEvidence(Base, IdMixin, CreatedMixin):
    """Revision-aware traceability. Valid at revision r: introduced <= r < coalesce(retired, inf)."""

    __tablename__ = "requirement_evidence"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_evidence_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    relation: Mapped[EvidenceRelation] = mapped_column(
        Enum(EvidenceRelation, name="evidence_relation"), nullable=False
    )
    introduced_in_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    retired_in_revision: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_req_evidence_requirement",
        ),
        ForeignKeyConstraint(
            ["project_id", "source_evidence_id"],
            ["source_evidence.project_id", "source_evidence.id"],
            name="fk_req_evidence_source",
        ),
        UniqueConstraint(
            "requirement_id", "source_evidence_id", "relation", "introduced_in_revision",
            name="uq_req_evidence",
        ),
        CheckConstraint(
            "retired_in_revision IS NULL OR retired_in_revision > introduced_in_revision",
            name="retired_after_introduced",
        ),
    )


class RequirementHistory(Base, IdMixin):
    """Append-only (trigger). Snapshot embeds children and valid evidence-link ids."""

    __tablename__ = "requirement_history"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    history_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    content_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    change_type: Mapped[ChangeType] = mapped_column(Enum(ChangeType, name="change_type"))
    snapshot: Mapped[dict] = mapped_column(JsonType, nullable=False)
    diff: Mapped[dict | None] = mapped_column(JsonType)
    cause_type: Mapped[CauseType] = mapped_column(Enum(CauseType, name="cause_type"))
    cause_id: Mapped[str | None] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(32), default="system")
    actor_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_req_history_requirement",
        ),
        UniqueConstraint("requirement_id", "history_seq", name="uq_req_history_seq"),
    )


class RequirementIssue(Base, IdMixin, TimestampMixin):
    __tablename__ = "requirement_issues"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    issue_type: Mapped[IssueType] = mapped_column(Enum(IssueType, name="issue_type"))
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    status: Mapped[IssueStatus] = mapped_column(
        Enum(IssueStatus, name="issue_status"), default=IssueStatus.open
    )
    resolution_text: Mapped[str | None] = mapped_column(Text)
    resolved_by_feedback_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    related_input_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "id", name="uq_requirement_issues_project_id_id"),
        UniqueConstraint("project_id", "fingerprint", name="uq_requirement_issues_fingerprint"),
        Index("ix_requirement_issues_status", "project_id", "status"),
    )


class RequirementIssueLink(Base):
    __tablename__ = "requirement_issue_links"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    issue_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    requirement_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "issue_id"],
            ["requirement_issues.project_id", "requirement_issues.id"],
            name="fk_issue_links_issue",
        ),
        ForeignKeyConstraint(
            ["project_id", "requirement_id"],
            ["requirements.project_id", "requirements.id"],
            name="fk_issue_links_requirement",
        ),
    )


class RequirementCandidate(Base, IdMixin, CreatedMixin):
    __tablename__ = "requirement_candidates"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    input_source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    chunk_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, nullable=False)
    disposition: Mapped[CandidateDisposition] = mapped_column(
        Enum(CandidateDisposition, name="candidate_disposition"),
        default=CandidateDisposition.pending,
    )
    merged_requirement_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    __table_args__ = (
        _project_fk(),
        UniqueConstraint("project_id", "chunk_key", name="uq_candidates_chunk"),
    )


class Stakeholder(Base, IdMixin):
    __tablename__ = "project_stakeholders"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    interest: Mapped[str | None] = mapped_column(Text)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (_project_fk(),)


class GlossaryTerm(Base, IdMixin):
    __tablename__ = "glossary_terms"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    term_en: Mapped[str] = mapped_column(String(255), nullable=False)
    term_bn: Mapped[str | None] = mapped_column(String(255))
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (_project_fk(), UniqueConstraint("project_id", "term_en", name="uq_glossary"))


class ProjectRisk(Base, IdMixin):
    __tablename__ = "project_risks"
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    risk: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    mitigation: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (_project_fk(),)
