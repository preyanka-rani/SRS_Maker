"""Per-process token-bucket throttle: the layer below Celery's task rate_limit."""
import threading
import time
from collections.abc import Callable


class CallThrottle:
    """Spaces calls so no more than `max_per_minute` start within a minute."""

    def __init__(self, max_per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if max_per_minute <= 0:
            raise ValueError("max_per_minute must be positive")
        self._interval = 60.0 / max_per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_at = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a slot is available. Returns seconds waited."""
        with self._lock:
            now = self._clock()
            start = max(now, self._next_at)
            self._next_at = start + self._interval
            wait = start - now
        if wait > 0:
            self._sleep(wait)
        return max(wait, 0.0)
