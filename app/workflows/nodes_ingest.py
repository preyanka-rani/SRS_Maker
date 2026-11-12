"""Graph nodes for the ingestion branch: routing, consent gate, extraction + persistence."""
import uuid
from datetime import UTC, datetime

from langgraph.types import interrupt

from app.database.models import ConsentScope, InputSource
from app.services import event_log, extraction_service, issue_service
from app.services.llm_service import GuardedLlm, has_consent
from app.workflows.deps import WorkflowDeps
from app.workflows.state import SrsWorkflowState

HELD = "held"


def _pid(state: SrsWorkflowState) -> uuid.UUID:
    return uuid.UUID(state["project_id"])


def _run_id(state: SrsWorkflowState) -> uuid.UUID | None:
    rid = (state.get("trigger") or {}).get("run_id")
    return uuid.UUID(rid) if rid else None


def make_ingest_nodes(deps: WorkflowDeps) -> dict:
    def load_project(state: SrsWorkflowState) -> dict:
        with deps.session_scope() as s:
            count = issue_service.blocking_open_issue_count(s, _pid(state))
        return {"open_blocking_issue_count": count, "route": None}

    def route_event(state: SrsWorkflowState) -> dict:
        kind = (state.get("trigger") or {}).get("type")
        route = {"document": "document", "start": "document", "email": "email",
                 "manual_decision": "manual", "consent_granted": "document"}.get(kind, "idle")
        return {"route": route}

    def consent_gate(state: SrsWorkflowState) -> dict:
        with deps.session_scope() as s:
            if has_consent(s, _pid(state), ConsentScope.text_processing):
                return {"route": "consented"}
            issue_service.open_manual_review(
                s, _pid(state), "LLM_CONSENT_REQUIRED", details="text_processing",
                entity_type="project", entity_id=state["project_id"])
            event_log.log_event(s, _pid(state), "held_no_consent", run_id=_run_id(state))
        return {"route": HELD}

    def extract_and_persist(state: SrsWorkflowState) -> dict:
        ref = (state.get("trigger") or {}).get("ref_id")
        if not ref:
            return {"changed_requirement_ids": []}
        project_id = _pid(state)
        with deps.session_scope() as s:
            llm = GuardedLlm(s, deps.llm_client(), project_id, _run_id(state))
            report = extraction_service.ExtractionReport()
            source_id = uuid.UUID(ref)
            cands = extraction_service.extract_candidates(s, llm, project_id, [source_id],
                                                          _run_id(state), report)
            extraction_service.merge_candidates(s, llm, project_id, cands, _run_id(state), report)
            source = s.get(InputSource, source_id)
            source.consumed_by_workflow_at = datetime.now(UTC).replace(tzinfo=None)
            event_log.log_event(
                s, project_id, "requirements_extracted", ref_type="input_source", ref_id=ref,
                run_id=_run_id(state),
                details={"created": report.created, "merged": report.merged,
                         "conflicts": [list(c) for c in report.conflicts],
                         "rejected": report.rejected, "flags": sorted(report.injection_flags)})
            if report.injection_flags:
                issue_service.raise_issue(
                    s, project_id, extraction_service.IssueType.injection_suspected,
                    "Possible prompt-injection content in a source",
                    description=", ".join(sorted(report.injection_flags)), key=f"inj:{ref}",
                    related_input_source_id=source_id)
            changed = report.created + report.merged
        return {"changed_requirement_ids": changed}

    def await_client(state: SrsWorkflowState) -> dict:
        # No side effects before interrupt(): this node re-executes from its start on resume.
        resume = interrupt({"waiting_for": "client"})
        return {"trigger": resume, "route": None}

    return {"load_project": load_project, "route_event": route_event,
            "consent_gate": consent_gate, "extract_and_persist": extract_and_persist,
            "await_client": await_client}
