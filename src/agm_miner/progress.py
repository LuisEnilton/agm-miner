"""Live progress of a stage: what it is doing, how far it got and what each worker is busy with.

Stages report into a :class:`Progress` (thread-safe, no I/O). Its JSON :meth:`Progress.snapshot`
is what every view shows: the live panel in the terminal running the command, the heartbeat that
``runs.track_run`` writes to ``pipeline_runs.progress`` and therefore ``agm status`` in another
terminal. Stages never print progress themselves.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class Progress:
    """Progress of one stage run.

    * ``phase``: what the stage is doing now ("searching filename:CLAUDE.md size:2048..4095");
    * ``waiting``: why it is idle and until when (API pacing, exhausted quota, retry back-off);
    * counters: named ``done``/``total`` pairs (``total`` may be unknown), e.g. repositories;
    * workers: one entry per parallel slot with the item, its phase and when it started.
    """

    def __init__(self, stage: str = "", *, clock: Callable[[], float] = time.time) -> None:
        self.stage = stage
        self._clock = clock
        self._lock = threading.Lock()
        self._started_at = clock()
        self._phase: str | None = None
        self._waiting: dict[str, Any] | None = None
        self._counters: dict[str, dict[str, int | None]] = {}
        self._workers: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ stage level

    def set_phase(self, phase: str | None) -> None:
        with self._lock:
            self._phase = phase

    def set_total(self, name: str, total: int | None) -> None:
        with self._lock:
            self._counter(name)["total"] = total

    def advance(self, name: str, amount: int = 1) -> None:
        with self._lock:
            counter = self._counter(name)
            counter["done"] = (counter["done"] or 0) + amount

    def set_done(self, name: str, done: int) -> None:
        with self._lock:
            self._counter(name)["done"] = done

    def _counter(self, name: str) -> dict[str, int | None]:
        return self._counters.setdefault(name, {"done": 0, "total": None})

    @contextmanager
    def waiting(self, reason: str, seconds: float) -> Iterator[None]:
        """Mark the stage as idle for ``seconds`` (e.g. around a ``sleep``)."""
        with self._lock:
            self._waiting = {"reason": reason, "until": _iso(self._clock() + seconds)}
        try:
            yield
        finally:
            with self._lock:
                self._waiting = None

    # ------------------------------------------------------------------ workers

    def start_work(self, slot: str, label: str, phase: str | None = None) -> None:
        with self._lock:
            self._workers[slot] = {
                "label": label,
                "phase": phase,
                "detail": None,
                "started_at": _iso(self._clock()),
            }

    def work_phase(self, slot: str, phase: str, detail: str | None = None) -> None:
        with self._lock:
            worker = self._workers.get(slot)
            if worker is not None:
                worker["phase"] = phase
                worker["detail"] = detail

    def end_work(self, slot: str) -> None:
        with self._lock:
            self._workers.pop(slot, None)

    # ------------------------------------------------------------------ views

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stage": self.stage,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": _iso(self._started_at),
                "updated_at": _iso(self._clock()),
                "phase": self._phase,
                "waiting": dict(self._waiting) if self._waiting else None,
                "counters": {name: dict(value) for name, value in self._counters.items()},
                "workers": [
                    {"slot": slot, **worker} for slot, worker in sorted(self._workers.items())
                ],
            }
