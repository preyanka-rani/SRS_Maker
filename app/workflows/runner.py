"""run_project_workflow: serialize per project, drain pending inputs oldest-first, resume safely.

Ordering and safety come from the DATABASE, not from Celery delivery order:
  * a session-level advisory lock gives mutual exclusion (a busy lock => caller retries);
  * `next_trigger` always picks the oldest unprocessed input from domain tables;
  * LangGraph checkpoints let a crashed run resume from its last completed node;
  * every side-effecting node is idempotent, so replays are harmless.
"""
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.database.models import WorkflowRun
from app.database.session import get_engine, session_scope
from app.integrations.storage.local import LocalStorage
from app.services import event_log
from app.workflows.deps import WorkflowDeps
from app.workflows.graph import build_graph
from app.workflows.nodes_ingest import HELD
from app.workflows.queue import next_trigger

MAX_ITEMS_PER_RUN = 200
_setup_done: set[str] = set()
_setup_lock = threading.Lock()


def default_deps(settings: Settings | None = None) -> WorkflowDeps:
    settings = settings or get_settings()

    def make_llm():
        from app.llm.client import GeminiClient

        if settings.gemini_api_key is None or not settings.gemini_model_reasoning:
            raise RuntimeError("GEMINI_API_KEY and GEMINI_MODEL_REASONING must be configured")
        return GeminiClient(settings.gemini_api_key.get_secret_value(),
                            settings.gemini_model_reasoning, settings.gemini_timeout_seconds,
                            settings.gemini_max_retries, settings.gemini_max_rpm)

    return WorkflowDeps(settings, LocalStorage(settings.storage_path), session_scope, make_llm)


def _psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://")


@contextmanager
def checkpointer(settings: Settings):
    url = _psycopg_url(settings.database_url)
    with PostgresSaver.from_conn_string(url) as saver:
        with _setup_lock:
            if url not in _setup_done:
                saver.setup()
                _setup_done.add(url)
        yield saver


@contextmanager
def project_lock(project_id: uuid.UUID):
    """Session-level advisory lock held for the whole run. Yields False when busy."""
    conn = get_engine().connect().execution_options(isolation_level="AUTOCOMMIT")
    key = f"project:{project_id}"
    try:
        got = conn.execute(text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": key}).scalar()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": key})
    finally:
        conn.close()


def prepare_checkpoints(settings: Settings | None = None) -> None:
    """Create the LangGraph tables once, at start-up, before any workflow thread runs."""
    with checkpointer(settings or get_settings()):
        pass


def waiting_point(snapshot) -> str | None:
    """'await_x' when paused at an interrupt, 'crashed' when stopped mid-run, None otherwise."""
    if not snapshot.next:
        return None
    for task in snapshot.tasks:
        if getattr(task, "interrupts", None):
            return task.name
    return "crashed"


def _new_run(project_id: uuid.UUID, thread_id: str, task_id: str | None, trigger: dict) -> str:
    with session_scope() as s:
        run = WorkflowRun(project_id=project_id, thread_id=thread_id, celery_task_id=task_id,
                          trigger_type=trigger["type"], trigger_ref=trigger.get("ref_id"))
        s.add(run)
        s.flush()
        event_log.log_event(s, project_id, "run_started", ref_type=trigger["type"],
                            ref_id=trigger.get("ref_id"), run_id=run.id, task_id=task_id)
        return str(run.id)


def _finish_run(run_id: str, status: str, error: str | None = None, node: str | None = None) -> None:
    with session_scope() as s:
        run = s.get(WorkflowRun, uuid.UUID(run_id))
        run.status, run.error, run.current_node = status, error, node
        run.finished_at = datetime.now(UTC).replace(tzinfo=None)


def run_for_project(project_id: uuid.UUID, celery_task_id: str | None = None,
                    deps: WorkflowDeps | None = None) -> str:
    """Returns 'busy' | 'idle' | 'held' | 'processed'. Raises if a graph node fails (Celery retries)."""
    deps = deps or default_deps()
    thread_id = f"project:{project_id}"
    config = {"configurable": {"thread_id": thread_id}}
    with project_lock(project_id) as acquired:
        if not acquired:
            return "busy"
        processed = 0
        with checkpointer(deps.settings) as saver:
            graph = build_graph(deps, saver)
            while processed < MAX_ITEMS_PER_RUN:
                snapshot = graph.get_state(config)
                waiting = waiting_point(snapshot)
                if waiting == "crashed":  # a previous run died mid-graph: continue from checkpoint
                    run_id = _new_run(project_id, thread_id, celery_task_id,
                                      {"type": "requeue", "ref_id": None})
                    _execute(graph, None, config, run_id)
                    processed += 1
                    continue
                with deps.session_scope() as s:
                    trigger = next_trigger(s, project_id, waiting)
                if trigger is None:
                    break
                run_id = _new_run(project_id, thread_id, celery_task_id, trigger)
                trigger = {**trigger, "run_id": run_id}
                payload = (Command(resume=trigger) if waiting
                           else {"project_id": str(project_id), "workflow_run_id": run_id,
                                 "trigger": trigger})
                result = _execute(graph, payload, config, run_id)
                processed += 1
                if (result or {}).get("route") == HELD:
                    return "held"
                with deps.session_scope() as s:  # guard against a trigger that was never consumed
                    again = next_trigger(s, project_id, waiting_point(graph.get_state(config)))
                if again == {k: v for k, v in trigger.items() if k != "run_id"}:
                    event_log_stalled(project_id, trigger)
                    return "stalled"
        return "processed" if processed else "idle"


def event_log_stalled(project_id: uuid.UUID, trigger: dict) -> None:
    with session_scope() as s:
        event_log.log_event(s, project_id, "run_stalled", ref_type=trigger.get("type"),
                            ref_id=trigger.get("ref_id"))


def _execute(graph, payload, config, run_id: str):
    try:
        result = graph.invoke(payload, config)
    except Exception as exc:
        _finish_run(run_id, "failed", f"{type(exc).__name__}: {exc}"[:2000])
        raise
    snap = graph.get_state(config)
    _finish_run(run_id, "waiting" if snap.next else "completed",
                node=snap.next[0] if snap.next else None)
    return result
