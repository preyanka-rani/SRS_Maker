"""Graph nodes after extraction: SRS generation, client-email handling, approval, manual decisions."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.database.models import (
    EmailMessage,
    ExtractionStatus,
    InputSource,
    ManualReview,
    Project,
    ProjectStatus,
    VersionStatus,
)
from app.domain import approval_rules
from app.services import (
    approval_service,
    event_log,
    feedback_service,
    ingestion_service,
    issue_service,
    srs_service,
)
from app.services.llm_service import GuardedLlm
from app.workflows.deps import WorkflowDeps
from app.workflows.state import SrsWorkflowState

IN_FLIGHT = (ExtractionStatus.pending, ExtractionStatus.processing, ExtractionStatus.transcribing)


def _pid(state: SrsWorkflowState) -> uuid.UUID:
    return uuid.UUID(state["project_id"])


def _run_id(state: SrsWorkflowState) -> uuid.UUID | None:
    rid = (state.get("trigger") or {}).get("run_id")
    return uuid.UUID(rid) if rid else None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sources_in_flight(session, project_id: uuid.UUID) -> bool:
    return session.execute(select(InputSource.id).where(
        InputSource.project_id == project_id,
        InputSource.extraction_status.in_(IN_FLIGHT)).limit(1)).first() is not None


def make_srs_nodes(deps: WorkflowDeps) -> dict:
    def generate_srs(state: SrsWorkflowState) -> dict:
        """Build (or reuse) the current draft and queue the review email."""
        project_id = _pid(state)
        with deps.session_scope() as s:
            if ingestion_service.pending_text_sources(s, project_id) or _sources_in_flight(s, project_id):
                return {"srs_version_id": None}  # more input is on its way; generate after the last one
            project = s.get(Project, project_id)
            llm = GuardedLlm(s, deps.llm_client(), project_id, _run_id(state))
            version = srs_service.generate_draft(s, deps.storage, deps.settings, llm, project,
                                                 _run_id(state))
            if version is None:
                return {"srs_version_id": None}
            if version.status == VersionStatus.ready:
                if project.auto_send_review:
                    srs_service.queue_review_email(s, deps.storage, deps.settings, project, version)
                else:
                    issue_service.open_manual_review(
                        s, project_id, "SEND_REVIEW_APPROVAL", entity_type="srs_version",
                        entity_id=str(version.id), workflow_run_id=_run_id(state))
            return {"srs_version_id": str(version.id)}

    def process_email(state: SrsWorkflowState) -> dict:
        ref = (state.get("trigger") or {}).get("ref_id")
        if not ref:
            return {"route": "idle"}
        project_id = _pid(state)
        with deps.session_scope() as s:
            email = s.get(EmailMessage, uuid.UUID(ref))
            project = s.get(Project, project_id)
            llm = GuardedLlm(s, deps.llm_client(), project_id, _run_id(state))
            result = feedback_service.analyze_email(s, deps.storage, deps.settings, llm, project,
                                                    email, _run_id(state))
            if result.route in ("changed",):
                project.status = ProjectStatus.revising
            return {"route": result.route, "feedback_record_id": str(result.feedback_id or ""),
                    "changed_requirement_ids": list(result.changed_codes)}

    def process_approval(state: SrsWorkflowState) -> dict:
        ref = (state.get("trigger") or {}).get("ref_id")
        project_id = _pid(state)
        with deps.session_scope() as s:
            email = s.get(EmailMessage, uuid.UUID(ref))
            project = s.get(Project, project_id)
            llm = GuardedLlm(s, deps.llm_client(), project_id, _run_id(state))
            outcome = approval_service.process_approval(s, deps.storage, deps.settings, llm, project,
                                                        email, _run_id(state))
        return {"route": "changed" if outcome == approval_rules.REVISION else "done"}

    def apply_manual_decision(state: SrsWorkflowState) -> dict:
        ref = (state.get("trigger") or {}).get("ref_id")
        project_id = _pid(state)
        with deps.session_scope() as s:
            review = s.get(ManualReview, uuid.UUID(ref)) if ref else None
            if review is None or review.status != "resolved":
                return {"route": "done"}
            decision = review.decision
            route = "changed" if decision == "regenerate" else "done"
            if decision == "accept_approval" and review.related_entity_type == "email":
                email = s.get(EmailMessage, uuid.UUID(review.related_entity_id))
                project = s.get(Project, project_id)
                llm = GuardedLlm(s, deps.llm_client(), project_id, _run_id(state))
                approval_service.process_approval(s, deps.storage, deps.settings, llm, project, email,
                                                  _run_id(state))
            review.status = "applied"
            event_log.log_event(s, project_id, "manual_decision_applied", ref_type="manual_review",
                                ref_id=ref, run_id=_run_id(state), details={"decision": decision})
        return {"route": route}

    return {"generate_srs": generate_srs, "process_email": process_email,
            "process_approval": process_approval, "apply_manual_decision": apply_manual_decision}
