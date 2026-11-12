import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.database.models import ReqType


class RequirementSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    req_type: str
    category: str | None
    title: str
    statement: str
    status: str
    priority: str | None
    content_revision: int
    history_seq: int
    lock_version: int


class AcceptanceIn(BaseModel):
    title: str | None = None
    given: str = Field(min_length=1)
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)


class RequirementCreate(BaseModel):
    req_type: ReqType
    category: str | None = Field(default=None, max_length=16)
    title: str = Field(min_length=3, max_length=200)
    statement: str = Field(min_length=10)
    rationale: str | None = None
    priority: str | None = Field(default=None, pattern="^(must|should|could|wont)$")
    details: dict[str, Any] | None = None
    acceptance_criteria: list[AcceptanceIn] = []


class RequirementPatch(BaseModel):
    reason: str = Field(min_length=3)
    title: str | None = Field(default=None, min_length=3, max_length=200)
    statement: str | None = Field(default=None, min_length=10)
    rationale: str | None = None
    priority: str | None = Field(default=None, pattern="^(must|should|could|wont)$")
    details: dict[str, Any] | None = None
    acceptance_criteria: list[AcceptanceIn] | None = None


class ReasonBody(BaseModel):
    reason: str = Field(min_length=3)


class IssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    issue_type: str
    blocking: bool
    title: str
    description: str | None
    why_it_matters: str | None
    status: str
    resolution_text: str | None
    created_at: datetime


class IssuePatch(BaseModel):
    status: str = Field(pattern="^(open|clarification_requested|resolved|dismissed)$")
    resolution_text: str | None = None


class ManualReviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    reason_code: str
    details: str | None
    status: str
    decision: str | None
    created_at: datetime


class ManualDecision(BaseModel):
    decision: str = Field(min_length=2, max_length=32)
    note: str = Field(min_length=1)
