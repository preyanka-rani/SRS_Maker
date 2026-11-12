"""LangGraph state: IDs and routing facts only. Text, files and candidates live in Postgres."""
import operator
from typing import Annotated, Literal, TypedDict

TriggerType = Literal["start", "document", "email", "manual_decision", "consent_granted",
                      "regenerate", "requeue"]


class WorkflowTrigger(TypedDict, total=False):
    type: TriggerType
    ref_id: str | None  # input_source_id / email_message_id / manual_review_id
    run_id: str | None  # workflow_runs.id this trigger executes under


class NodeError(TypedDict):
    node: str
    code: str
    message: str


class SrsWorkflowState(TypedDict, total=False):
    project_id: str
    workflow_run_id: str
    trigger: WorkflowTrigger
    route: str | None
    input_source_ids: list[str]
    changed_requirement_ids: list[str]
    open_blocking_issue_count: int
    feedback_record_id: str | None
    feedback_category: str | None
    srs_version_id: str | None
    last_sent_srs_version_id: str | None
    approved_srs_version_id: str | None
    final_srs_version_id: str | None
    generation_attempts: int
    approval_id: str | None
    manual_review_id: str | None
    errors: Annotated[list[NodeError], operator.add]
