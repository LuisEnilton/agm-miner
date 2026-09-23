"""``agm run``: run discovery, selection and mining in parallel, then export and analyze.

Each stage runs as its own ``agm <stage>`` child process, so every guarantee of the individual
commands still holds (stage locks, per-repository locks, resumable progress, clean interruption).
The supervisor only decides *when* to start each stage, from the repository counts in the
database:

* ``discover`` runs once (unless disabled);
* ``select`` is started again, at most every ``select_interval`` seconds, while repositories are
  ``DISCOVERED``;
* ``mine`` is started again, at most every ``mine_interval`` seconds, while repositories are
  ``SELECTED``;
* when discovery has finished and nothing is left to select or mine, ``export`` and ``analyze``
  run one after the other.

Children run in their own process group; the supervisor forwards each Ctrl+C/SIGTERM it
receives to them exactly once, so one signal lets them stop gracefully and a second one aborts
(same semantics as ``agm mine``). A child that fails stops the scheduling of new work.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TextIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from agm_miner.database.models import Repository, RepositoryStatus

logger = logging.getLogger(__name__)

INTERRUPTED_EXIT_CODE = 130
# A child may also die from the signal itself if it arrives before its handlers are installed.
_STOPPED_BY_SIGNAL = frozenset({INTERRUPTED_EXIT_CODE, -signal.SIGINT, -signal.SIGTERM})

CommandBuilder = Callable[[str, Sequence[str]], list[str]]


def agm_command(stage: str, args: Sequence[str]) -> list[str]:
    """``agm <stage> <args>`` with the current interpreter (works inside any virtualenv)."""
    return [sys.executable, "-m", "agm_miner.cli", stage, *args]


@dataclass(frozen=True, slots=True)
class PipelineOptions:
    workers: int = 4
    discover: bool = True
    export: bool = True
    analyze: bool = True
    select_interval: float = 60.0
    mine_interval: float = 60.0
    status_interval: float = 60.0
    poll_interval: float = 1.0
    study_path: Path | None = None
    queries: Sequence[str] = ()

    def study_args(self) -> list[str]:
        return ["--study", str(self.study_path)] if self.study_path else []


@dataclass(slots=True)
class PipelineResult:
    exit_code: int
    stages: list[tuple[str, int]] = field(default_factory=list)  # (stage, exit code) in order
    failed_stage: str | None = None
    interrupted: bool = False
    final_counts: dict[str, int] = field(default_factory=dict)


class _Child:
    """A running ``agm <stage>`` process whose output is relayed with a ``[stage]`` prefix."""

    def __init__(self, stage: str, command: list[str], output: TextIO, lock: threading.Lock):
        self.stage = stage
        self.signals_delivered = 0
        environment = dict(os.environ, PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=environment,
            start_new_session=True,  # terminal signals reach only the supervisor
        )
        self._relay = threading.Thread(
            target=self._copy, args=(self.process.stdout, output, lock), daemon=True
        )
        self._relay.start()

    def _copy(self, stream: IO[str], output: TextIO, lock: threading.Lock) -> None:
        for line in stream:
            with lock:
                output.write(f"[{self.stage}] {line}")
                output.flush()
        stream.close()

    def poll(self) -> int | None:
        code = self.process.poll()
        if code is not None:
            self._relay.join()
        return code

    def deliver_signals(self, received: int) -> None:
        """Forward every signal the supervisor received that this child has not seen yet."""
        while self.signals_delivered < received and self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            self.signals_delivered += 1


def status_counts(session_factory: sessionmaker[Session]) -> dict[str, int]:
    with session_factory() as session:
        rows = session.execute(
            select(Repository.status, func.count()).group_by(Repository.status)
        ).all()
    counts = {status.value: 0 for status in RepositoryStatus}
    counts.update({status.value: count for status, count in rows})
    return counts


class PipelineSupervisor:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        options: PipelineOptions,
        *,
        command: CommandBuilder = agm_command,
        output: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._options = options
        self._command = command
        self._output = output or sys.stderr
        self._output_lock = threading.Lock()
        self._clock = clock
        self._sleep = sleep
        self._children: dict[str, _Child] = {}
        self._signals = 0
        self._result = PipelineResult(exit_code=0)

    # ------------------------------------------------------------------ entry point

    def run(self) -> PipelineResult:
        with self._forward_signals():
            self._parallel_phase()
            if self._result.failed_stage is None and not self._result.interrupted:
                self._final_phase()
        self._result.final_counts = status_counts(self._session_factory)
        if self._result.interrupted:
            self._result.exit_code = INTERRUPTED_EXIT_CODE
        elif self._result.failed_stage is not None:
            self._result.exit_code = 1
        return self._result

    # ------------------------------------------------------------------ phases

    def _parallel_phase(self) -> None:
        options = self._options
        discovery_done = not options.discover
        if options.discover:
            queries = [argument for query in options.queries for argument in ("--query", query)]
            self._start("discover", [*queries, *options.study_args()])
        next_start = {"select": 0.0, "mine": 0.0}
        next_status = 0.0

        while True:
            for stage, code in self._reap():
                if stage == "discover" and code == 0:
                    discovery_done = True
            if self._stopping():
                if not self._children:
                    return
                self._sleep(options.poll_interval)
                continue

            counts = status_counts(self._session_factory)
            now = self._clock()
            if (
                "select" not in self._children
                and counts[RepositoryStatus.DISCOVERED.value] > 0
                and now >= next_start["select"]
            ):
                self._start("select", options.study_args())
                next_start["select"] = now + options.select_interval
            if (
                "mine" not in self._children
                and counts[RepositoryStatus.SELECTED.value] > 0
                and now >= next_start["mine"]
            ):
                self._start("mine", ["--workers", str(options.workers), *options.study_args()])
                next_start["mine"] = now + options.mine_interval

            drained = (
                counts[RepositoryStatus.DISCOVERED.value] == 0
                and counts[RepositoryStatus.SELECTED.value] == 0
            )
            if discovery_done and drained and not self._children:
                self._log(f"collection finished: {self._describe(counts)}")
                return
            if now >= next_status:
                self._log(self._describe(counts))
                next_status = now + options.status_interval
            self._sleep(options.poll_interval)

    def _final_phase(self) -> None:
        # analyze reads the latest export: without a fresh one it would analyze stale data.
        analyze = self._options.analyze and self._options.export
        for stage, enabled in (("export", self._options.export), ("analyze", analyze)):
            if not enabled:
                continue
            self._start(stage, self._options.study_args())
            while stage in self._children:
                self._reap()
                self._sleep(self._options.poll_interval)
            if self._stopping():
                return

    # ------------------------------------------------------------------ children

    def _start(self, stage: str, args: list[str]) -> None:
        command = self._command(stage, args)
        self._log(f"starting {stage}")
        self._children[stage] = _Child(stage, command, self._output, self._output_lock)

    def _reap(self) -> list[tuple[str, int]]:
        """Deliver pending signals and collect the children that exited."""
        finished = []
        for child in self._children.values():
            child.deliver_signals(self._signals)
        for stage, child in list(self._children.items()):
            code = child.poll()
            if code is None:
                continue
            del self._children[stage]
            finished.append((stage, code))
            self._result.stages.append((stage, code))
            if code == 0:
                self._log(f"{stage} finished")
            elif self._signals and code in _STOPPED_BY_SIGNAL:
                self._log(f"{stage} stopped")
            elif self._result.failed_stage is None:
                self._result.failed_stage = stage
                self._log(f"{stage} failed with exit code {code}; no new work will be started")
        return finished

    def _stopping(self) -> bool:
        return self._result.failed_stage is not None or self._result.interrupted

    # ------------------------------------------------------------------ signals

    @contextmanager
    def _forward_signals(self) -> Iterator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        # The handler only counts; the main loop forwards (see _reap), so a child started at
        # the same moment as the signal still receives it.
        def handler(signum, frame) -> None:
            self._signals += 1
            self._result.interrupted = True
            action = "stopping gracefully" if self._signals == 1 else "aborting"
            self._log(f"signal received: {action} (forwarded to running stages)")

        previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)

    # ------------------------------------------------------------------ output

    def _describe(self, counts: dict[str, int]) -> str:
        running = ", ".join(sorted(self._children)) or "none"
        states = " ".join(f"{status}={counts[status]}" for status in counts)
        return f"status: {states} | running: {running}"

    def _log(self, message: str) -> None:
        with self._output_lock:
            self._output.write(f"[run] {message}\n")
            self._output.flush()


def run_pipeline(
    session_factory: sessionmaker[Session], options: PipelineOptions, **kwargs
) -> PipelineResult:
    return PipelineSupervisor(session_factory, options, **kwargs).run()
