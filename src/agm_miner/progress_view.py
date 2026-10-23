"""Terminal rendering of progress snapshots (``progress.Progress.snapshot``).

The same renderer serves the live panel of a running command, ``agm status`` and the panel of
``agm run``, so the three always look the same.
"""

from __future__ import annotations

import io
import logging
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.logging import RichHandler
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from agm_miner.database.models import PipelineRun
from agm_miner.progress import published
from agm_miner.runs import is_stale


@dataclass(frozen=True, slots=True)
class RunView:
    """One stage run to display: its progress snapshot plus what the database knows about it."""

    stage: str
    progress: Mapping[str, Any] | None
    run_id: int | None = None
    stale: bool = False
    heartbeat_at: datetime | None = None
    stats: Mapping[str, Any] = field(default_factory=dict)
    note: str | None = None


def run_view(run: PipelineRun, now: datetime | None = None) -> RunView:
    """View of a ``PipelineRun`` row (progress saved by its heartbeat)."""
    return RunView(
        stage=run.stage.value,
        progress=run.progress,
        run_id=run.id,
        stale=is_stale(run, now),
        heartbeat_at=run.heartbeat_at,
        stats=run.stats or {},
    )


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _counter_row(
    name: str, counter: Mapping[str, Any], elapsed: float
) -> tuple[RenderableType, ...]:
    done = counter.get("done") or 0
    total = counter.get("total")
    rate = done / elapsed * 60 if elapsed > 0 else 0.0
    if total:
        bar: RenderableType = ProgressBar(total=total, completed=min(done, total), width=28)
        amount = f"{done:,}/{total:,} ({done / total:.0%})"
        remaining = (total - done) / (done / elapsed) if done and elapsed > 0 else None
        eta = f"eta {format_duration(remaining)}" if remaining is not None else "eta —"
    else:
        bar = Text("")
        amount = f"{done:,}"
        eta = ""
    return (Text(name, style="bold"), bar, Text(amount), Text(f"{rate:,.1f}/min"), Text(eta))


def render_run(view: RunView, now: datetime | None = None) -> RenderableType:
    now = now or datetime.now(UTC)
    snapshot = view.progress or {}
    started = _parse(snapshot.get("started_at"))
    elapsed = (now - started).total_seconds() if started else 0.0

    title = Text(view.stage.lower(), style="bold cyan")
    if view.run_id is not None:
        title.append(f" #{view.run_id}", style="cyan")
    if started:
        age = format_duration(elapsed)
        title.append(f"  started {age} ago" if view.stale else f"  running for {age}")
    if snapshot.get("pid"):
        title.append(f"  pid {snapshot['pid']}@{snapshot.get('host', '?')}", style="dim")
    parts: list[RenderableType] = [title]

    if view.note:
        parts.append(Text(f"  {view.note}", style="dim"))
    if view.stale:
        silent = (now - view.heartbeat_at).total_seconds() if view.heartbeat_at else None
        since = f"for {format_duration(silent)}" if silent is not None else "since it started"
        parts.append(
            Text(f"  no sign of life {since}: the process was most likely killed", style="red")
        )
    if snapshot.get("phase"):
        parts.append(Text(f"  {snapshot['phase']}"))
    waiting = snapshot.get("waiting")
    if waiting:
        until = _parse(waiting.get("until"))
        left = f" ({format_duration((until - now).total_seconds())} left)" if until else ""
        parts.append(Text(f"  waiting: {waiting['reason']}{left}", style="yellow"))

    counters = snapshot.get("counters") or {}
    if counters:
        grid = Table.grid(padding=(0, 2))
        for _ in range(5):
            grid.add_column()
        for name, counter in counters.items():
            grid.add_row(*_counter_row(name, counter, elapsed))
        parts.append(_indented(grid))

    workers = snapshot.get("workers") or []
    if workers:
        table = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
        for column in ("worker", "item", "phase", "detail", "time"):
            table.add_column(column)
        for worker in workers:
            started_at = _parse(worker.get("started_at"))
            table.add_row(
                str(worker.get("slot", "")),
                str(worker.get("label", "")),
                str(worker.get("phase") or ""),
                str(worker.get("detail") or ""),
                format_duration((now - started_at).total_seconds()) if started_at else "",
            )
        parts.append(_indented(table))
    return Group(*parts)


def _indented(renderable: RenderableType) -> RenderableType:
    grid = Table.grid()
    grid.add_column(width=2)
    grid.add_column()
    grid.add_row("", renderable)
    return grid


def render_status(
    runs: Sequence[RunView],
    repository_counts: Mapping[str, int] | None = None,
    now: datetime | None = None,
) -> RenderableType:
    """Every running stage, then the repository counts per status."""
    parts: list[RenderableType] = []
    if not runs:
        parts.append(Text("No stage is running.", style="dim"))
    for view in runs:
        parts.append(render_run(view, now))
        parts.append(Text(""))
    if repository_counts:
        footer = Text("repositories: ", style="bold")
        footer.append(
            "  ".join(f"{status} {count:,}" for status, count in repository_counts.items() if count)
            or "none"
        )
        parts.append(footer)
    return Group(*parts)


def summary_line(snapshot: Mapping[str, Any]) -> str:
    """One log line with the essentials of a snapshot (for logs written to files or pipes)."""
    parts = [f"[{str(snapshot.get('stage', '')).lower()}]"]
    for name, counter in (snapshot.get("counters") or {}).items():
        done, total = counter.get("done") or 0, counter.get("total")
        parts.append(f"{name} {done:,}/{total:,}" if total else f"{name} {done:,}")
    workers = snapshot.get("workers") or []
    if workers:
        busy = ", ".join(
            f"{worker.get('label')} ({worker.get('phase') or 'starting'})" for worker in workers
        )
        parts.append(f"working on {busy}")
    elif snapshot.get("phase"):
        parts.append(str(snapshot["phase"]))
    waiting = snapshot.get("waiting")
    if waiting:
        parts.append(f"waiting: {waiting['reason']}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- terminal output

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# In "plain" mode, how often a summary line of every running stage is logged.
PLAIN_SUMMARY_SECONDS = 30.0

_summary_logger = logging.getLogger("agm_miner.progress")


def resolve_mode(mode: str, stream: TextIO | None = None) -> str:
    """``auto`` becomes ``live`` in an interactive terminal and ``plain`` otherwise."""
    if mode != "auto":
        return mode
    stream = stream or sys.stderr
    return "live" if stream.isatty() else "plain"


# Libraries that log every HTTP request at INFO (one line per LLM call would bury the stage's
# own messages and scroll the live panel); their warnings and errors still show.
QUIET_LIBRARIES = ("httpx", "httpx2", "google_genai", "anthropic")


def configure_logging(level: str, mode: str) -> Console | None:
    """Set up logging for ``mode``; in ``live`` mode, log lines scroll above the panel and the
    returned console is the one the panel must use."""
    for library in QUIET_LIBRARIES:
        logging.getLogger(library).setLevel(max(logging.WARNING, logging.getLevelName(level)))
    if mode == "live":
        console = Console(stderr=True)
        handler = RichHandler(console=console, show_path=False, markup=False)
        logging.basicConfig(level=level, format="%(message)s", handlers=[handler], force=True)
        return console
    logging.basicConfig(level=level, format=LOG_FORMAT)
    return None


class _LiveView:
    """Renderable of the stages running in this process, for ``rich.live.Live``."""

    def __init__(self) -> None:
        self._shown = False

    def __call__(self) -> RenderableType:
        progresses = published()
        if not progresses:
            return Text("finishing…" if self._shown else "starting…", style="dim")
        self._shown = True
        return render_status([RunView(p.stage, p.snapshot()) for p in progresses])


@contextmanager
def showing_progress(
    mode: str,
    console: Console | None = None,
    *,
    summary_seconds: float = PLAIN_SUMMARY_SECONDS,
) -> Iterator[None]:
    """Show the progress of the stages running in this process while the block runs: a live
    panel (``live``), a summary log line every ``summary_seconds`` (``plain``) or nothing."""
    if mode == "live":
        with Live(
            get_renderable=_LiveView(),
            console=console or Console(stderr=True),
            refresh_per_second=2,
            transient=True,
        ):
            yield
        return
    if mode != "plain":
        yield
        return
    stopped = threading.Event()

    def report() -> None:
        while not stopped.wait(summary_seconds):
            for progress in published():
                _summary_logger.info("%s", summary_line(progress.snapshot()))

    reporter = threading.Thread(target=report, name="progress-summary", daemon=True)
    reporter.start()
    try:
        yield
    finally:
        stopped.set()
        reporter.join(timeout=1)


# --------------------------------------------------------------------------- agm run


def render_pipeline(
    children: Mapping[str, int],
    runs: Mapping[str, PipelineRun],
    repository_counts: Mapping[str, int],
    now: datetime | None = None,
    *,
    finished: frozenset[int] = frozenset(),
) -> RenderableType:
    """Panel of ``agm run``: each child stage with the progress its run saved in the database.

    ``finished`` holds the pids of children whose run already ended (the process is exiting).
    """
    views = []
    for stage in sorted(children):
        run = runs.get(stage)
        if run is not None:
            views.append(run_view(run, now))
        elif stage == "analyze":
            views.append(RunView(stage, None, note="its progress is not saved (no database)"))
        elif children[stage] in finished:
            views.append(RunView(stage, None, note="finishing…"))
        else:
            views.append(RunView(stage, None, note="starting…"))
    return render_status(views, repository_counts, now)


class PipelinePanel:
    """Live panel of ``agm run``, built from the supervisor's children and the database."""

    def __init__(
        self,
        children: Callable[[], Mapping[str, int]],
        runs: Callable[[Mapping[str, int]], Mapping[str, PipelineRun]],
        counts: Callable[[], Mapping[str, int]],
    ) -> None:
        self._children = children
        self._runs = runs
        self._counts = counts
        self._seen: set[int] = set()  # pids of children that had a running run

    def __call__(self) -> RenderableType:
        children = self._children()
        runs = self._runs(children)
        self._seen.update(children[stage] for stage in runs)
        finished = frozenset(pid for pid in children.values() if pid in self._seen) - {
            children[stage] for stage in runs
        }
        return render_pipeline(children, runs, self._counts(), finished=finished)


class ConsoleWriter(io.TextIOBase):
    """Text stream that prints through a rich console, so lines written while a live panel is
    shown scroll above it (used for the children's output in ``agm run``)."""

    def __init__(self, console: Console) -> None:
        super().__init__()
        self._console = console

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        for line in text.splitlines():
            self._console.print(line, markup=False, highlight=False, soft_wrap=True)
        return len(text)


class _Refreshing:
    """Renderable built by ``build``; keeps the last good frame if building fails (e.g. the
    database is briefly unreachable), so the panel never crashes the command."""

    def __init__(self, build: Callable[[], RenderableType]) -> None:
        self._build = build
        self._last: RenderableType = Text("starting…", style="dim")

    def __call__(self) -> RenderableType:
        with suppress(Exception):
            self._last = self._build()
        return self._last


@contextmanager
def live_panel(
    build: Callable[[], RenderableType], console: Console, refresh_per_second: float = 1
) -> Iterator[None]:
    """Show ``build()`` as a live panel (refreshed in the background) while the block runs."""
    with Live(
        get_renderable=_Refreshing(build),
        console=console,
        refresh_per_second=refresh_per_second,
        transient=True,
    ):
        yield
