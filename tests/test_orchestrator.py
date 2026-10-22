"""`agm run` supervisor, exercised with real child processes (fake discover/select/mine, real
export and analyze) against the test database."""

from __future__ import annotations

import io
import json
import os
import signal
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from agm_miner.database.models import Repository, RepositoryStatus
from agm_miner.orchestrator import PipelineOptions, agm_command, run_pipeline

pytestmark = pytest.mark.db

FAKE_STAGE = Path(__file__).with_name("fake_stage.py")
FAKE_STAGES = {"discover", "select", "mine"}


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("FAKE_STAGE_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    return tmp_path


def command(stage: str, args):
    if stage in FAKE_STAGES:
        return [sys.executable, str(FAKE_STAGE), stage, *args]
    return agm_command(stage, args)


def options(**overrides) -> PipelineOptions:
    values = {
        "workers": 2,
        "select_interval": 0.1,
        "mine_interval": 0.1,
        "status_interval": 1.0,
        "poll_interval": 0.05,
    }
    values.update(overrides)
    return PipelineOptions(**values)


def behave(monkeypatch, **behaviour) -> None:
    monkeypatch.setenv("FAKE_STAGE", json.dumps(behaviour))


def statuses(session_factory) -> dict[int, RepositoryStatus]:
    with session_factory() as session:
        return dict(session.execute(select(Repository.id, Repository.status)).all())


def test_runs_stages_in_parallel_until_done_then_exports_and_analyzes(
    session_factory, workdir, monkeypatch
) -> None:
    behave(monkeypatch, discover={"repositories": 6, "delay": 0.15})
    output = io.StringIO()
    result = run_pipeline(session_factory, options(), command=command, output=output)

    assert result.exit_code == 0, output.getvalue()
    assert result.failed_stage is None and not result.interrupted
    assert set(statuses(session_factory).values()) == {
        RepositoryStatus.MINED,
        RepositoryStatus.REJECTED,
    }
    names = [stage for stage, _ in result.stages]
    assert names[-2:] == ["export", "analyze"]
    assert all(code == 0 for _, code in result.stages)
    # select/mine ran while discovery was still producing repositories.
    assert names.index("discover") > names.index("select")
    assert list((workdir / "data" / "gold").iterdir())
    log = output.getvalue()
    assert "[discover] fake discover started" in log and "collection finished" in log


def test_mine_receives_workers_and_study_arguments(session_factory, workdir, monkeypatch) -> None:
    behave(monkeypatch, discover={"repositories": 1})
    study = workdir / "study.yaml"
    study.write_text("version: 1\n")
    result = run_pipeline(
        session_factory,
        options(workers=7, study_path=study, queries=("filename:AGENTS.md",), export=False),
        command=command,
        output=io.StringIO(),
    )
    assert result.exit_code == 0
    calls = (workdir / "calls.log").read_text().splitlines()
    assert f"discover --query filename:AGENTS.md --study {study}" in calls
    assert f"mine --workers 7 --study {study}" in calls


def test_failed_stage_stops_new_work_and_skips_export(
    session_factory, workdir, monkeypatch
) -> None:
    behave(monkeypatch, discover={"repositories": 3}, select={"exit": 1})
    output = io.StringIO()
    result = run_pipeline(session_factory, options(), command=command, output=output)

    assert result.exit_code == 1
    assert result.failed_stage == "select"
    assert "export" not in [stage for stage, _ in result.stages]
    assert "select failed with exit code 1" in output.getvalue()


def test_drain_only_mode_without_discovery(session_factory, workdir, monkeypatch) -> None:
    with session_factory() as session, session.begin():
        session.add(
            Repository(id=1, node_id="R_1", full_name="org/a", status=RepositoryStatus.SELECTED)
        )
    behave(monkeypatch)
    result = run_pipeline(
        session_factory,
        options(discover=False, export=False),
        command=command,
        output=io.StringIO(),
    )
    assert result.exit_code == 0
    assert result.stages == [("mine", 0)]
    assert statuses(session_factory) == {1: RepositoryStatus.MINED}


def test_nothing_to_do_finishes_immediately(session_factory, workdir, monkeypatch) -> None:
    behave(monkeypatch)
    result = run_pipeline(
        session_factory,
        options(discover=False, export=False),
        command=command,
        output=io.StringIO(),
    )
    assert result.exit_code == 0 and result.stages == []


@pytest.mark.parametrize("signals", [1, 2])
def test_each_signal_is_forwarded_exactly_once(
    session_factory, workdir, monkeypatch, signals: int
) -> None:
    with session_factory() as session, session.begin():
        session.add(
            Repository(id=1, node_id="R_1", full_name="org/a", status=RepositoryStatus.SELECTED)
        )
    behave(monkeypatch, mine={"wait_for_signals": signals})

    def press_ctrl_c() -> None:
        started = workdir / "mine.started"
        for _ in range(200):
            if started.exists():
                break
            threading.Event().wait(0.05)
        for _ in range(signals):
            os.kill(os.getpid(), signal.SIGINT)
            threading.Event().wait(0.2)

    presser = threading.Thread(target=press_ctrl_c)
    presser.start()
    output = io.StringIO()
    result = run_pipeline(session_factory, options(discover=False), command=command, output=output)
    presser.join()

    assert result.exit_code == 130 and result.interrupted
    assert result.failed_stage is None, output.getvalue()
    assert (workdir / "mine.signals").read_text() == str(signals)
    assert "export" not in [stage for stage, _ in result.stages]
    assert "mine stopped" in output.getvalue()


def test_analysis_needs_a_fresh_export(session_factory, workdir, monkeypatch) -> None:
    """Without an export in this run, analyze would read a stale one, so it does not run."""
    behave(monkeypatch)
    result = run_pipeline(
        session_factory,
        options(discover=False, export=False, analyze=True),
        command=command,
        output=io.StringIO(),
    )
    assert result.stages == []


def test_children_leave_progress_display_to_the_supervisor(
    session_factory, workdir, monkeypatch
) -> None:
    """Children run with AGM_PROGRESS=off; the supervisor logs their progress, read from the
    run each child saved in the database."""
    behave(monkeypatch, discover={"repositories": 2}, mine={"report_progress": 1.0})
    output = io.StringIO()
    result = run_pipeline(
        session_factory,
        options(status_interval=0.2, export=False),
        command=command,
        output=output,
    )
    assert result.exit_code == 0, output.getvalue()
    assert (workdir / "mine.progress_mode").read_text() == "off"
    assert "[run] progress [mine] · repositories 1/2 · fake mine working" in output.getvalue()


def test_status_lines_without_progress(session_factory, workdir, monkeypatch) -> None:
    behave(monkeypatch, discover={"repositories": 2}, mine={"report_progress": 1.0})
    output = io.StringIO()
    run_pipeline(
        session_factory,
        options(status_interval=0.2, export=False),
        command=command,
        output=output,
        progress_lines=False,
    )
    assert "[run] status:" in output.getvalue()
    assert "[run] progress" not in output.getvalue()
