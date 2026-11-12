import uuid

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.database.models import (
    ExtractionStatus,
    InputSource,
    ManualReview,
    ProjectLlmConsent,
    Requirement,
    SourceType,
    WorkflowEventLog,
    WorkflowRun,
)
from app.database.session import get_engine, session_scope
from app.integrations.storage.local import LocalStorage
from app.llm.client import FakeLlmClient, LlmError
from app.llm.output_schemas import ExtractionOutput, RequirementCandidateDraft
from app.services import ingestion_service
from app.workflows import runner
from app.workflows.deps import WorkflowDeps
from tests.helpers import docx_bytes, make_project


def _draft(statement, title="Feature"):
    return RequirementCandidateDraft(
        req_type="FR", title=title, statement=statement, evidence_refs=["E1"], confidence=0.9,
        actors="User", acceptance_criteria=[{"given": "g", "when": "w", "then": "t"}])


@pytest.fixture
def wf(app_db, tmp_path):
    settings = get_settings()
    storage = LocalStorage(tmp_path / "wfstore")
    fake = FakeLlmClient()
    deps = WorkflowDeps(settings, storage, session_scope, lambda: fake)
    with session_scope() as s:
        project = make_project(s)
        pid = project.id
    yield deps, fake, pid, tmp_path
    with get_engine().begin() as c:  # clear LangGraph checkpoints + app tables for isolation
        c.execute(text("TRUNCATE checkpoint_writes, checkpoint_blobs, checkpoints CASCADE"))


def grant(pid, scope="text_processing"):
    with session_scope() as s:
        s.add(ProjectLlmConsent(project_id=pid, scope=scope, policy_reference="DPA"))


def add_doc(deps, pid, tmp_path, text_value):
    data = docx_bytes(tmp_path, [text_value], f"{uuid.uuid4().hex[:6]}.docx")
    with session_scope() as s:
        res = ingestion_service.register_source(s, deps.storage, deps.settings, pid, data,
                                                "d.docx", SourceType.upload)
        ingestion_service.ingest_text_source(s, deps.storage, deps.settings, res.source.id)
        return res.source.id


def reqs(pid):
    with session_scope() as s:
        return [r.code + ":" + r.statement for r in s.execute(
            select(Requirement).where(Requirement.project_id == pid).order_by(Requirement.code)).scalars()]


def test_processes_pending_documents_oldest_first_and_pauses_for_client(wf):
    deps, fake, pid, tmp = wf
    grant(pid)
    first = add_doc(deps, pid, tmp, "first doc")
    second = add_doc(deps, pid, tmp, "second doc")
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[_draft("Customers shall pay invoices by credit card.")]))
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[_draft("Administrators shall export audit logs nightly.")]))
    assert runner.run_for_project(pid, "t1", deps) == "processed"
    assert reqs(pid) == ["FR-001:Customers shall pay invoices by credit card.",
                         "FR-002:Administrators shall export audit logs nightly."]
    with session_scope() as s:
        consumed = [s.get(InputSource, i).consumed_by_workflow_at for i in (first, second)]
        runs = s.execute(select(WorkflowRun).where(WorkflowRun.project_id == pid)).scalars().all()
        events = s.execute(select(WorkflowEventLog.event_type).where(
            WorkflowEventLog.project_id == pid)).scalars().all()
    assert all(consumed) and consumed[0] <= consumed[1]
    assert len(runs) == 2 and {"run_started", "requirements_extracted"} <= set(events)
    # the graph is parked at an interrupt, not holding a worker
    with runner.checkpointer(deps.settings) as saver:
        from app.workflows.graph import build_graph

        snap = build_graph(deps, saver).get_state({"configurable": {"thread_id": f"project:{pid}"}})
        assert runner.waiting_point(snap) == "await_client"


def test_idle_when_nothing_pending_and_duplicate_kicks_are_harmless(wf):
    deps, fake, pid, tmp = wf
    grant(pid)
    add_doc(deps, pid, tmp, "only doc")
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[_draft("Managers shall approve leave requests online.")]))
    assert runner.run_for_project(pid, "t1", deps) == "processed"
    calls = len(fake.calls)
    assert runner.run_for_project(pid, "t2", deps) == "idle"
    assert runner.run_for_project(pid, "t3", deps) == "idle"
    assert len(fake.calls) == calls and len(reqs(pid)) == 1


def test_without_consent_input_is_held_not_consumed_then_processed_after_grant(wf):
    deps, fake, pid, tmp = wf
    src = add_doc(deps, pid, tmp, "held doc")
    assert runner.run_for_project(pid, "t1", deps) == "held"
    assert fake.calls == []
    with session_scope() as s:
        assert s.get(InputSource, src).consumed_by_workflow_at is None
        assert s.execute(select(ManualReview).where(ManualReview.project_id == pid,
                                                    ManualReview.reason_code == "LLM_CONSENT_REQUIRED")).scalars().all()
    grant(pid)
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[_draft("Drivers shall upload delivery photos.")]))
    assert runner.run_for_project(pid, "t2", deps) == "processed"
    assert reqs(pid) == ["FR-001:Drivers shall upload delivery photos."]


def test_busy_lock_returns_busy_without_doing_work(wf):
    deps, fake, pid, tmp = wf
    grant(pid)
    add_doc(deps, pid, tmp, "doc")
    with runner.project_lock(pid) as held:
        assert held
        assert runner.run_for_project(pid, "t1", deps) == "busy"
    assert fake.calls == []
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[]))
    assert runner.run_for_project(pid, "t2", deps) == "processed"


def test_crash_mid_run_resumes_from_checkpoint_without_duplicates(wf):
    deps, fake, pid, tmp = wf
    grant(pid)
    add_doc(deps, pid, tmp, "first")
    add_doc(deps, pid, tmp, "second")
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[_draft("Teachers shall record attendance daily.")]))
    fake.enqueue(ExtractionOutput, LlmError("gemini_http_503", "boom", retryable=True))  # 2nd doc fails
    with pytest.raises(LlmError):
        runner.run_for_project(pid, "t1", deps)
    assert reqs(pid) == ["FR-001:Teachers shall record attendance daily."]  # first committed, second rolled back
    with session_scope() as s:
        statuses = [r.status for r in s.execute(select(WorkflowRun).where(
            WorkflowRun.project_id == pid).order_by(WorkflowRun.started_at)).scalars()]
    assert statuses == ["waiting", "failed"]
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[_draft("Parents shall receive grade notifications.")]))
    assert runner.run_for_project(pid, "t2", deps) == "processed"  # resumes the crashed node
    assert reqs(pid) == ["FR-001:Teachers shall record attendance daily.", "FR-002:Parents shall receive grade notifications."]
    assert runner.run_for_project(pid, "t3", deps) == "idle"


def test_documents_of_other_projects_are_never_touched(wf):
    deps, fake, pid, tmp = wf
    with session_scope() as s:
        other = make_project(s).id
    grant(pid)
    other_src = add_doc(deps, other, tmp, "other project doc")
    fake.enqueue(ExtractionOutput, ExtractionOutput(requirements=[]))
    assert runner.run_for_project(pid, "t1", deps) == "idle"
    with session_scope() as s:
        assert s.get(InputSource, other_src).consumed_by_workflow_at is None
        assert s.get(InputSource, other_src).extraction_status == ExtractionStatus.succeeded
