"""Progress model, its rendering, the heartbeat saved by track_run and `agm status`."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console
from sqlalchemy import select
from typer.testing import CliRunner

from agm_miner.cli import app
from agm_miner.config import StudyConfig
from agm_miner.database.models import PipelineRun, RunStage, RunStatus
from agm_miner.progress import Progress
from agm_miner.progress_view import RunView, format_duration, render_status
from agm_miner.runs import is_stale, track_run

T0 = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0.timestamp()

    def __call__(self) -> float:
        return self.now


def text(renderable, width: int = 120) -> str:
    console = Console(record=True, width=width, color_system=None)
    console.print(renderable)
    return console.export_text()


def test_progress_snapshot() -> None:
    clock = Clock()
    progress = Progress("MINE", clock=clock)
    progress.set_phase("mining 3 repositories")
    progress.set_total("repositories", 3)
    progress.advance("repositories")
    progress.start_work("worker-1", "acme/api", "clone")
    progress.work_phase("worker-1", "prefetch blobs", "312/980")
    progress.start_work("worker-2", "acme/web")
    progress.end_work("worker-2")
    clock.now += 30
    with progress.waiting("API pacing", 6.5):
        waiting = progress.snapshot()["waiting"]
    snapshot = progress.snapshot()

    assert waiting == {"reason": "API pacing", "until": (T0 + timedelta(seconds=36.5)).isoformat()}
    assert snapshot["waiting"] is None
    assert snapshot["stage"] == "MINE" and snapshot["phase"] == "mining 3 repositories"
    assert snapshot["counters"] == {"repositories": {"done": 1, "total": 3}}
    assert snapshot["workers"] == [
        {
            "slot": "worker-1",
            "label": "acme/api",
            "phase": "prefetch blobs",
            "detail": "312/980",
            "started_at": T0.isoformat(),
        }
    ]
    assert (
        snapshot["pid"] > 0 and snapshot["updated_at"] == (T0 + timedelta(seconds=30)).isoformat()
    )
    json.dumps(snapshot)  # stored as JSONB


def test_progress_is_thread_safe() -> None:
    progress = Progress()

    def work(slot: int) -> None:
        for _ in range(2000):
            progress.advance("items")
            progress.start_work(f"w{slot}", "x")
            progress.snapshot()
            progress.end_work(f"w{slot}")

    threads = [threading.Thread(target=work, args=(slot,)) for slot in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert progress.snapshot()["counters"]["items"]["done"] == 8000


def test_format_duration() -> None:
    assert [format_duration(s) for s in (0, 59, 61, 3600, 3725)] == [
        "0s",
        "59s",
        "1m01s",
        "1h00m",
        "1h02m",
    ]


def test_render_status() -> None:
    clock = Clock()
    progress = Progress("MINE", clock=clock)
    progress.set_phase("mining 4 repositories")
    progress.set_total("repositories", 4)
    progress.advance("repositories", 2)
    progress.start_work("worker-1", "acme/api", "prefetch blobs")
    progress.work_phase("worker-1", "prefetch blobs", "312/980")
    discover = Progress("DISCOVER", clock=clock)
    discover.advance("pages", 7)
    with discover.waiting("search quota exhausted", 40):
        discover_snapshot = discover.snapshot()
    now = T0 + timedelta(seconds=120)

    output = text(
        render_status(
            [
                RunView("MINE", progress.snapshot(), run_id=12),
                RunView("DISCOVER", discover_snapshot, run_id=13),
                RunView(
                    "SELECT",
                    Progress("SELECT", clock=clock).snapshot(),
                    run_id=9,
                    stale=True,
                    heartbeat_at=now - timedelta(minutes=5),
                ),
            ],
            {"SELECTED": 141, "MINED": 284, "FAILED": 0},
            now=now,
        )
    )
    assert "mine #12  running for 2m00s" in output
    assert "mining 4 repositories" in output
    assert "2/4 (50%)" in output and "1.0/min" in output and "eta 2m00s" in output
    assert "acme/api" in output and "prefetch blobs" in output and "312/980" in output
    assert "pages" in output and "7" in output
    assert "waiting: search quota exhausted" in output
    assert "select #9  started 2m00s ago" in output
    assert "no sign of life for 5m00s: the process was most likely killed" in output
    assert "repositories: SELECTED 141  MINED 284" in output and "FAILED" not in output
    assert "No stage is running." in text(render_status([], {}))


@pytest.mark.db
def test_track_run_saves_progress_while_running(session_factory) -> None:
    def saved() -> PipelineRun:
        with session_factory() as session:
            return session.scalars(select(PipelineRun)).one()

    with track_run(session_factory, RunStage.MINE, StudyConfig(), heartbeat_seconds=0.05) as run:
        started = saved()
        assert started.heartbeat_at is not None and started.progress["stage"] == "MINE"
        run.increment("mined")
        run.progress.set_total("repositories", 2)
        run.progress.advance("repositories")
        for _ in range(100):
            current = saved()
            if current.stats:
                break
            threading.Event().wait(0.05)
        # Stats and progress are visible to other processes before the run finishes.
        assert current.status is RunStatus.RUNNING
        assert current.stats == {"mined": 1}
        assert current.progress["counters"]["repositories"] == {"done": 1, "total": 2}
        assert current.heartbeat_at >= started.heartbeat_at
        assert not is_stale(current)

    finished = saved()
    assert finished.status is RunStatus.SUCCEEDED and finished.heartbeat_at == finished.finished_at


@pytest.mark.db
def test_status_reports_running_and_stale_runs(session_factory) -> None:
    now = datetime.now(UTC)
    common = {
        "code_version": "x",
        "study_config_hash": "h",
        "study_config": {},
        "status": RunStatus.RUNNING,
    }
    with session_factory() as session, session.begin():
        session.add(
            PipelineRun(
                stage=RunStage.MINE,
                heartbeat_at=now,
                progress=Progress("MINE").snapshot(),
                stats={"mined": 3},
                **common,
            )
        )
        # Killed with kill -9 long ago, and a run recorded before heartbeats existed.
        session.add(
            PipelineRun(stage=RunStage.SELECT, heartbeat_at=now - timedelta(hours=1), **common)
        )
        session.add(PipelineRun(stage=RunStage.DISCOVER, **common))
        session.add(
            PipelineRun(
                stage=RunStage.EXPORT,
                status=RunStatus.SUCCEEDED,
                **{k: v for k, v in common.items() if k != "status"},
            )
        )

    runner = CliRunner()
    result = runner.invoke(app, ["status", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [(r["stage"], r["stale"]) for r in payload["running"]] == [
        ("MINE", False),
        ("SELECT", True),
        ("DISCOVER", True),
    ]
    assert payload["running"][0]["stats"] == {"mined": 3}
    assert payload["repositories"]["SELECTED"] == 0

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "mine #" in result.output and "no sign of life" in result.output

    listed = json.loads(runner.invoke(app, ["runs", "list"]).output)
    assert {run["stage"]: run["stale"] for run in listed} == {
        "MINE": False,
        "SELECT": True,
        "DISCOVER": True,
        "EXPORT": False,
    }


def test_render_pipeline() -> None:
    from agm_miner.progress_view import render_pipeline

    clock = Clock()
    progress = Progress("MINE", clock=clock)
    progress.set_total("repositories", 2)
    progress.advance("repositories")
    mine = PipelineRun(
        id=5,
        stage=RunStage.MINE,
        status=RunStatus.RUNNING,
        progress=progress.snapshot(),
        heartbeat_at=T0,
        stats={},
    )
    output = text(
        render_pipeline(
            {"mine": 100, "analyze": 101, "select": 102},
            {"mine": mine},
            {"SELECTED": 3},
            now=T0 + timedelta(seconds=10),
        )
    )
    assert "mine #5" in output and "1/2 (50%)" in output
    assert "analyze" in output and "its progress is not saved (no database)" in output
    assert "select" in output and "starting…" in output
    assert "repositories: SELECTED 3" in output


def test_console_writer_prints_each_line() -> None:
    from agm_miner.progress_view import ConsoleWriter

    console = Console(record=True, width=80, color_system=None)
    writer = ConsoleWriter(console)
    writer.write("[mine] one\n[mine] [bold]two[/bold]\n")
    assert console.export_text() == "[mine] one\n[mine] [bold]two[/bold]\n"


def test_pipeline_panel_tells_finishing_from_starting() -> None:
    from agm_miner.progress_view import PipelinePanel

    mine = PipelineRun(
        id=5,
        stage=RunStage.MINE,
        status=RunStatus.RUNNING,
        progress=Progress("MINE").snapshot(),
        heartbeat_at=datetime.now(UTC),
        stats={},
    )
    state = {"runs": {"mine": mine}}
    panel = PipelinePanel(
        lambda: {"mine": 100, "select": 101},
        lambda children: state["runs"],
        lambda: {},
    )
    first = text(panel())
    assert "mine #5" in first and "starting…" in first  # select has no run yet
    state["runs"] = {}  # mine's run ended; its process is still exiting
    second = text(panel())
    assert "finishing…" in second and "starting…" in second
