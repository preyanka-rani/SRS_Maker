"""Celery tasks. Domain logic lives in services; tasks only open a transaction and delegate.

After any commit that produces new pending input, a task kicks `run_project_workflow`, which
is idempotent: duplicate or out-of-order kicks just find nothing new to do.
"""
import uuid

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.models import ExtractionStatus, InputSource, ProcessingClass
from app.database.session import session_scope
from app.integrations.storage.local import LocalStorage
from app.integrations.transcription.base import TranscriptionError
from app.integrations.transcription.factory import get_transcription_provider
from app.services import ingestion_service
from app.workers.celery_app import celery_app

_RETRY_BACKOFF = (30, 120, 600, 1800, 3600)


def _storage() -> LocalStorage:
    return LocalStorage(get_settings().storage_path)


# Local mode (no Redis): tasks run eagerly. When the API process starts its scheduler, workflow
# kicks run on a small thread pool so HTTP requests never wait for LLM calls.
_background = {"executor": None}


def enable_background_kicks() -> None:
    from concurrent.futures import ThreadPoolExecutor

    _background["executor"] = ThreadPoolExecutor(max_workers=2, thread_name_prefix="workflow")


def kick_workflow(project_id: uuid.UUID | str) -> None:
    """Enqueue a (rate-limited, idempotent) workflow run for the project."""
    executor = _background["executor"]
    if executor is not None:
        executor.submit(run_project_workflow.apply, args=(str(project_id),))
    else:
        run_project_workflow.delay(str(project_id))


@celery_app.task(name="app.workers.tasks.ingest_document", bind=True, max_retries=3)
def ingest_document(self, source_id: str) -> str:
    settings = get_settings()
    with session_scope() as session:
        source = ingestion_service.ingest_text_source(session, _storage(), settings,
                                                      uuid.UUID(source_id))
        project_id = source.project_id
        status = source.extraction_status.value
    kick_workflow(project_id)
    return status


@celery_app.task(name="app.workers.tasks.transcribe_audio", bind=True, max_retries=5)
def transcribe_audio(self, source_id: str) -> str:
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        provider = get_transcription_provider(settings)
        with session_scope() as session:
            source = ingestion_service.transcribe_source(session, _storage(), settings, provider,
                                                         uuid.UUID(source_id))
            project_id, status = source.project_id, source.extraction_status.value
    except TranscriptionError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            delay = _RETRY_BACKOFF[min(self.request.retries, len(_RETRY_BACKOFF) - 1)]
            raise self.retry(exc=exc, countdown=delay) from exc
        raise
    kick_workflow(project_id)
    return status


def dispatch_source(source: InputSource) -> None:
    """Route a freshly registered source to the right queue."""
    if source.processing_class == ProcessingClass.text_supported:
        ingest_document.delay(str(source.id))
    elif source.processing_class == ProcessingClass.audio:
        transcribe_audio.delay(str(source.id))
    else:  # deferred/rejected sources still change what the workflow should report
        kick_workflow(source.project_id)


@celery_app.task(name="app.workers.tasks.run_project_workflow", bind=True, max_retries=40)
def run_project_workflow(self, project_id: str) -> str:
    from app.workflows.runner import run_for_project  # imported lazily: heavy + LLM-bound

    settings = get_settings()
    outcome = run_for_project(uuid.UUID(project_id), self.request.id)
    if outcome == "busy":
        if self.request.is_eager:  # the lock holder drains pending inputs; the scheduler nudges later
            return outcome
        raise self.retry(countdown=settings.workflow_lock_retry_seconds)
    return outcome


# ---- mailbox / outbox / housekeeping ---------------------------------------------------------

def handle_poll_outcomes(outcomes) -> int:
    """Dispatch freshly stored sources, then kick the workflow of every touched project."""
    projects: set[uuid.UUID] = set()
    for outcome in outcomes:
        if outcome.project_id is None or outcome.status not in ("new_request", "reply"):
            continue
        projects.add(outcome.project_id)
        for source_id in outcome.new_source_ids:
            with session_scope() as session:
                source = session.get(InputSource, source_id)
                if source is not None:
                    dispatch_source(source)
    for project_id in projects:
        kick_workflow(project_id)
    return len(projects)


@celery_app.task(name="app.workers.tasks.poll_imap")
def poll_imap() -> int:
    from app.services import email_service

    settings = get_settings()
    if not settings.mail_configured:
        return 0
    outcomes = email_service.poll_mailbox(session_scope, _storage(), settings)
    return handle_poll_outcomes(outcomes)


@celery_app.task(name="app.workers.tasks.dispatch_outbox")
def dispatch_outbox() -> dict:
    from app.services import outbox_service

    settings = get_settings()
    if not settings.mail_configured:
        return {}
    return outbox_service.dispatch_due(session_scope, _storage(), settings)


@celery_app.task(name="app.workers.tasks.nudge_pending_inputs")
def nudge_pending_inputs() -> int:
    """Recover from lost messages: kick every project that still has pending input."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.database.models import EmailMessage, MailStatus

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        minutes=get_settings().pending_input_nudge_minutes)
    with session_scope() as session:
        queued = set(session.execute(select(EmailMessage.project_id).where(
            EmailMessage.processing_status.in_([MailStatus.queued, MailStatus.processing]),
            EmailMessage.created_at < cutoff, EmailMessage.project_id.is_not(None))).scalars())
        sources = set(session.execute(select(InputSource.project_id).where(
            InputSource.consumed_by_workflow_at.is_(None),
            InputSource.extraction_status == ExtractionStatus.succeeded,
            InputSource.processing_class.in_([ProcessingClass.text_supported,
                                              ProcessingClass.audio]))).scalars())
    for project_id in queued | sources:
        kick_workflow(project_id)
    return len(queued | sources)


@celery_app.task(name="app.workers.tasks.retention_purge")
def retention_purge() -> int:
    """Delete temporary files older than 24 h. Records, approvals and audit data are kept."""
    import time

    root = _storage().root / "temp"
    removed = 0
    for path in root.rglob("*"):
        if path.is_file() and time.time() - path.stat().st_mtime > 86400:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
