"""Celery app. External AI calls are confined to two rate-limited queues:

  llm  -> run_project_workflow  (every Gemini call happens inside LangGraph nodes here)
  asr  -> transcribe_audio      (every Groq call happens here)
  default -> ingestion, email, outbox, retention

`rate_limit` is enforced per worker instance, so the deployment runs ONE worker-llm and ONE
worker-asr; provider clients add a per-process throttle underneath.
"""
from celery import Celery

from app.core.config import get_settings


def create_celery() -> Celery:
    settings = get_settings()
    app = Celery("srs", broker=settings.redis_url, backend=None)
    app.conf.update(
        task_default_queue="default",
        task_routes={
            "app.workers.tasks.run_project_workflow": {"queue": "llm"},
            "app.workers.tasks.transcribe_audio": {"queue": "asr"},
        },
        task_annotations={
            "app.workers.tasks.run_project_workflow": {"rate_limit": settings.celery_llm_task_rate_limit},
            "app.workers.tasks.transcribe_audio": {"rate_limit": settings.celery_asr_task_rate_limit},
        },
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        broker_transport_options={"visibility_timeout": 7200},
        task_always_eager=settings.celery_task_always_eager or settings.local_scheduler,
        task_eager_propagates=False,
        timezone="UTC",
        beat_schedule={
            "poll-imap": {"task": "app.workers.tasks.poll_imap",
                          "schedule": float(settings.imap_poll_interval_seconds)},
            "dispatch-outbox": {"task": "app.workers.tasks.dispatch_outbox", "schedule": 30.0},
            "nudge-pending": {"task": "app.workers.tasks.nudge_pending_inputs",
                              "schedule": float(settings.pending_input_nudge_minutes * 60)},
            "retention-purge": {"task": "app.workers.tasks.retention_purge", "schedule": 86400.0},
        },
    )
    app.conf.imports = ("app.workers.tasks",)
    return app


celery_app = create_celery()
