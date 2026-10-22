"""Terminal rendering of progress snapshots (``progress.Progress.snapshot``).

The same renderer serves the live panel of a running command, ``agm status`` and the panel of
``agm run``, so the three always look the same.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rich.console import Group, RenderableType
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from agm_miner.database.models import PipelineRun
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
