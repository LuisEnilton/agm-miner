"""Every stage reports its progress (saved with the run) and the terminal shows it."""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

import pytest
from rich.console import Console
from sqlalchemy import select

from agm_miner.analysis.datasets import build_datasets
from agm_miner.config import AnalysisConfig, Settings, StudyConfig
from agm_miner.database.models import PipelineRun, Repository, RepositoryStatus, RunStage
from agm_miner.progress import Progress, published, publishing
from agm_miner.progress_view import resolve_mode, showing_progress, summary_line
from agm_miner.stages.discovery import discover_repositories
from agm_miner.stages.export import export_bronze
from agm_miner.stages.mining import MiningOptions, mine_repositories
from agm_miner.stages.selection import select_repositories
from tests.gitrepo import RepoBuilder
from tests.test_discovery_stage import FakeSearch
from tests.test_selection_stage import FakeGraphQL, node


def final_progress(session_factory, stage: RunStage) -> dict:
    with session_factory() as session:
        run = session.scalars(
            select(PipelineRun).where(PipelineRun.stage == stage).order_by(PipelineRun.id.desc())
        ).first()
        return run.progress


def counters(progress: dict) -> dict:
    return {name: (c["done"], c["total"]) for name, c in progress["counters"].items()}


# --------------------------------------------------------------------------- stages


@pytest.mark.db
def test_discovery_progress_walks_the_estimated_population(session_factory) -> None:
    study = StudyConfig.model_validate(
        {"discovery": {"queries": ["filename:CLAUDE.md"], "size_min": 0, "size_max": 4999}}
    )
    fake = FakeSearch([(i + 1, i, "CLAUDE.md") for i in range(2500)])
    discover_repositories(study, session_factory, fake)
    progress = final_progress(session_factory, RunStage.DISCOVER)
    assert counters(progress)["queries"] == (1, 1)
    assert counters(progress)["matches (est.)"] == (2500, 2500)
    assert counters(progress)["new repositories"] == (2500, None)
    assert progress["phase"].startswith("filename:CLAUDE.md size:")

    # Resuming a finished discovery reaches the same total from the saved slices.
    discover_repositories(study, session_factory, fake)
    assert counters(final_progress(session_factory, RunStage.DISCOVER))["matches (est.)"] == (
        2500,
        2500,
    )


@pytest.mark.db
def test_discovery_progress_counts_unreachable_matches(session_factory) -> None:
    study = StudyConfig.model_validate(
        {"discovery": {"queries": ["filename:CLAUDE.md"], "size_min": 7, "size_max": 7}}
    )
    discover_repositories(
        study, session_factory, FakeSearch([(i + 1, 7, "CLAUDE.md") for i in range(1200)])
    )
    progress = counters(final_progress(session_factory, RunStage.DISCOVER))
    assert progress["unreachable"] == (200, None)
    assert progress["matches (est.)"] == (1200, 1200)


@pytest.mark.db
def test_discovery_progress_of_a_query_with_its_own_size(session_factory) -> None:
    fake = FakeSearch([(i + 1, 10, "CLAUDE.md") for i in range(150)])
    discover_repositories(
        StudyConfig(), session_factory, fake, queries=["filename:CLAUDE.md size:5..20"]
    )
    progress = final_progress(session_factory, RunStage.DISCOVER)
    assert counters(progress)["matches (est.)"] == (150, 150)
    assert progress["phase"] == "filename:CLAUDE.md size:5..20: page 2/2"


@pytest.mark.db
def test_selection_progress(session_factory) -> None:
    with session_factory() as session, session.begin():
        for repo_id in (1, 2, 3):
            session.add(
                Repository(id=repo_id, node_id=f"R_{repo_id}", full_name=f"org/repo{repo_id}")
            )
    fake = FakeGraphQL({f"R_{i}": node(i, stargazerCount=1 if i == 3 else 120) for i in (1, 2, 3)})
    study = StudyConfig.model_validate(
        {"selection": {"reference_date": "2026-06-01", "graphql_batch_size": 2}}
    )
    select_repositories(study, session_factory, fake)
    assert counters(final_progress(session_factory, RunStage.SELECT)) == {
        "repositories": (3, 3),
        "selected": (2, None),
        "rejected": (1, None),
    }


@pytest.mark.db
@pytest.mark.git
def test_mining_progress_reports_workers_and_phases(
    session_factory, repo_builder: RepoBuilder, tmp_path: Path, monkeypatch
) -> None:
    repo_builder.commit(
        "Add instructions", {"CLAUDE.md": "# Agent\n"}, date="2024-02-01T10:00:00+00:00"
    )
    with session_factory() as session, session.begin():
        for repo_id in (1, 2):
            session.add(
                Repository(
                    id=repo_id,
                    node_id=f"R_{repo_id}",
                    full_name=f"acme/r{repo_id}",
                    status=RepositoryStatus.SELECTED,
                )
            )
    phases: list[tuple[str, str]] = []
    original = Progress.work_phase

    def recording(self, slot, phase, detail=None):
        phases.append((slot, phase))
        original(self, slot, phase, detail)

    monkeypatch.setattr(Progress, "work_phase", recording)
    mine_repositories(
        Settings(CLONE_DIR=str(tmp_path / "clones")),
        StudyConfig(),
        session_factory,
        MiningOptions(workers=2),
        clone_url=lambda _: repo_builder.url,
    )
    progress = final_progress(session_factory, RunStage.MINE)
    assert counters(progress) == {"repositories": (2, 2), "mined": (2, None)}
    assert progress["workers"] == []  # every slot was released
    names = [phase for _, phase in phases]
    for expected in ("clone", "authors", "history", "artifacts at HEAD", "contents", "saving"):
        assert expected in names
    assert {slot for slot, _ in phases} <= {"worker_0", "worker_1"}


@pytest.mark.db
def test_export_progress_counts_tables_and_rows(session_factory, tmp_path: Path) -> None:
    with session_factory() as session, session.begin():
        session.add(Repository(id=1, node_id="R_1", full_name="org/a"))
    export_bronze(StudyConfig(), session_factory, tmp_path / "bronze")
    progress = counters(final_progress(session_factory, RunStage.EXPORT))
    tables_done, tables_total = progress["tables"]
    assert tables_done == tables_total > 10
    rows_done, rows_total = progress["rows"]
    # The export counts its own run row before writing it, so both sides agree.
    assert rows_done == rows_total >= 2


@pytest.mark.db
def test_analysis_progress_goes_through_every_step(session_factory, tmp_path: Path) -> None:
    bronze = export_bronze(StudyConfig(), session_factory, tmp_path / "bronze")
    progress = Progress("ANALYZE")
    build_datasets(bronze, tmp_path / "gold", AnalysisConfig(), progress)
    snapshot = progress.snapshot()
    assert counters(snapshot)["steps"] == (8, 8)
    assert counters(snapshot)["versions"] == (0, 0)
    assert snapshot["phase"] == "writing datasets"


# --------------------------------------------------------------------------- terminal


class Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_resolve_mode() -> None:
    assert resolve_mode("auto", Stream(tty=True)) == "live"
    assert resolve_mode("auto", Stream(tty=False)) == "plain"
    assert resolve_mode("off", Stream(tty=True)) == "off"


def test_summary_line() -> None:
    progress = Progress("MINE")
    progress.set_total("repositories", 10)
    progress.advance("repositories", 3)
    progress.start_work("worker_0", "acme/api", "clone")
    with progress.waiting("GitHub graphql pacing", 1):
        line = summary_line(progress.snapshot())
    assert line == (
        "[mine] · repositories 3/10 · working on acme/api (clone) · waiting: GitHub graphql pacing"
    )


def test_published_progress_is_scoped() -> None:
    progress = Progress("MINE")
    with publishing(progress):
        assert progress in published()
    assert progress not in published()


def test_plain_mode_logs_summaries(caplog) -> None:
    progress = Progress("MINE")
    progress.set_total("repositories", 4)
    caplog.set_level(logging.INFO, logger="agm_miner.progress")
    with publishing(progress), showing_progress("plain", summary_seconds=0.01):
        for _ in range(100):
            if caplog.records:
                break
            threading.Event().wait(0.02)
    assert any("[mine]" in record.getMessage() for record in caplog.records)


def test_live_mode_draws_the_panel() -> None:
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=100, color_system=None)
    progress = Progress("MINE")
    progress.start_work("worker_0", "acme/api", "clone")
    with publishing(progress), showing_progress("live", console):
        threading.Event().wait(0.7)  # at least one refresh
    assert "acme/api" in output.getvalue()
