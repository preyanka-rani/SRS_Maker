"""Versioned, strict schemas for every LLM output. The LLM only ever returns proposals."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

ReqTypeLit = Literal[
    "FR", "NFR", "BR", "SEC", "BUS_RULE", "INT", "CON", "ASM", "DEP",
    "REPORT", "UI_SCREEN", "SCOPE_EXCLUSION", "FUTURE_SCOPE", "TECH",
]
FeedbackCategoryLit = Literal[
    "NEW_REQUIREMENT", "MODIFY_REQUIREMENT", "REMOVE_REQUIREMENT", "CLARIFICATION_REQUEST",
    "REQUIREMENT_CONFIRMATION", "APPROVAL", "REJECTION", "GENERAL_QUESTION", "UNKNOWN",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class AcceptanceCriterionDraft(_Strict):
    scenario_title: str | None = None
    given: str = Field(min_length=1)
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)


class RequirementCandidateDraft(_Strict):
    req_type: ReqTypeLit
    category: str | None = Field(default=None, max_length=16)
    title: str = Field(min_length=3, max_length=200)
    statement: str = Field(min_length=10)  # English canonical
    rationale: str | None = None
    priority: Literal["must", "should", "could", "wont"] | None = None
    source_language: str | None = Field(default=None, max_length=8)
    evidence_refs: list[str] = Field(min_length=1)  # must come from the supplied allowlist
    confidence: float = Field(ge=0, le=1)
    purpose: str | None = None
    actors: str | None = None
    trigger: str | None = None
    preconditions: str | None = None
    main_flow: list[str] = []
    acceptance_criteria: list[AcceptanceCriterionDraft] = []
    ambiguity_note: str | None = None  # set when the statement is vague/untestable


class ExtractionOutput(_Strict):
    requirements: list[RequirementCandidateDraft] = []


class Adjudication(_Strict):
    """Judgement on one candidate pair."""

    candidate_ref: str
    existing_code: str
    relation: Literal["duplicate", "conflict", "related", "unrelated"]
    reason: str


class AdjudicationOutput(_Strict):
    judgements: list[Adjudication] = []


class ChangeItemDraft(_Strict):
    action: Literal["add", "modify", "remove", "clarify", "confirm"]
    target_code: str | None = None  # must be in the allowlist of existing codes
    req_type: ReqTypeLit | None = None
    category: str | None = Field(default=None, max_length=16)  # e.g. PERF, SEC, AVAIL for NFR
    title: str | None = None
    statement: str | None = None
    evidence_excerpt: str | None = None


class FeedbackOutput(_Strict):
    category: FeedbackCategoryLit
    secondary_categories: list[FeedbackCategoryLit] = []
    confidence: float = Field(ge=0, le=1)
    change_items: list[ChangeItemDraft] = []
    question_summary: str | None = None
    referenced_version: str | None = None


class ApprovalSignal(_Strict):
    """Signals only. Code, never the model, decides approval."""

    explicit_approval: bool
    approval_scope_all_requirements: bool
    has_change_requests: bool
    has_questions: bool
    states_version: str | None = None
    quoted_phrase: str | None = None
    confidence: float = Field(ge=0, le=1)


class SrsProse(_Strict):
    text: str = Field(min_length=1)
    cited_codes: list[str] = []


class SrsNarrativeOutput(_Strict):
    """Prose for the narrative sections only. Requirement text never comes from here."""

    executive_summary: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    business_context: str = Field(min_length=1)


class ReplyDraft(_Strict):
    """A short, polite English reply to a client question. Staff may review it first."""

    body: str = Field(min_length=1)
    needs_human: bool = False


class ClarifyingQuestion(_Strict):
    category: str
    question: str = Field(min_length=10, max_length=400)


class ClarifyingQuestionsOutput(_Strict):
    questions: list[ClarifyingQuestion] = []
