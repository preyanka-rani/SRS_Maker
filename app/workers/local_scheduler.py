"""In-process scheduler for local runs without Redis/beat: polls the mailbox, sends queued
mail and nudges stuck projects. Started by the API when LOCAL_SCHEDULER=true."""
import logging
import threading

from app.core.config import Settings
from app.workers import tasks

log = logging.getLogger("scheduler")


class LocalScheduler:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _loop(self, name: str, interval: float, fn, first_delay: float | None = None) -> None:
        wait = interval if first_delay is None else first_delay
        while not self._stop.wait(wait):
            wait = interval
            try:
                fn()
            except Exception:  # noqa: BLE001 - a failing job must never stop the scheduler
                log.exception("scheduled job failed: %s", name)

    def start(self) -> None:
        tasks.enable_background_kicks()
        s = self._settings
        jobs = [("poll_imap", float(s.imap_poll_interval_seconds), tasks.poll_imap, 5.0),
                ("dispatch_outbox", 15.0, tasks.dispatch_outbox, 5.0),
                ("nudge_pending", float(s.pending_input_nudge_minutes * 60), tasks.nudge_pending_inputs, 10.0),
                ("retention", 86400.0, tasks.retention_purge, None)]
        for name, interval, task, first in jobs:
            thread = threading.Thread(target=self._loop, args=(name, interval, task, first),
                                      name=f"sched-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)
        log.info("local scheduler started (mail_configured=%s)", s.mail_configured)

    def stop(self) -> None:
        self._stop.set()
