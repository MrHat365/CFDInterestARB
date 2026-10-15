"""Execution progress, independent of transport liveness and the broker lock."""
from __future__ import annotations

import threading
import time


class WorkerHealth:
    STARTUP_TIMEOUT = 120.0
    PROGRESS_TIMEOUT = 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._progress: dict[str, float] = {'initialize': self._started}

    def ready(self):
        with self._lock:
            now = time.monotonic()
            self._progress = {'orders': now, 'poll': now}

    def mark(self, service: str):
        with self._lock:
            self._progress[service] = time.monotonic()

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            ages = {name: max(0., now - stamp) for name, stamp in self._progress.items()}
            if ages:
                stalled = [name for name, age in ages.items()
                           if age >= (self.STARTUP_TIMEOUT if name == 'initialize' else self.PROGRESS_TIMEOUT)]
            else:
                stalled = ['initialize'] if now - self._started >= self.STARTUP_TIMEOUT else []
        return {'progress_age_seconds': ages, 'stalled': stalled,
                'healthy': not stalled}
