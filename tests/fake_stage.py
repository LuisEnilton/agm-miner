"""Stand-in for ``agm discover|select|mine`` used by the orchestrator tests.

Run as ``python tests/fake_stage.py <stage> [args...]``; it changes repository statuses in the
test database the way the real stage would. Behaviour is configured through ``FAKE_STAGE``
(JSON), e.g. ``{"select": {"exit": 1}}``, ``{"mine": {"wait_for_signals": 2}}`` or
``{"mine": {"report_progress": 1.0}}`` (save a RUNNING run with progress, then work 1 s).
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text


def main() -> int:
    stage = sys.argv[1]
    behaviour = json.loads(os.environ.get("FAKE_STAGE", "{}")).get(stage, {})
    workdir = Path(os.environ["FAKE_STAGE_DIR"])
    with (workdir / "calls.log").open("a") as log:
        log.write(" ".join(sys.argv[1:]) + "\n")
    (workdir / f"{stage}.progress_mode").write_text(os.environ.get("AGM_PROGRESS", ""))
    print(f"fake {stage} started", flush=True)
    if "exit" in behaviour:
        return int(behaviour["exit"])

    engine = create_engine(os.environ["DATABASE_URL"])
    if "report_progress" in behaviour:
        _report_progress(engine, stage)
        time.sleep(behaviour["report_progress"])
    if stage == "discover":
        for repo_id in range(1, behaviour.get("repositories", 5) + 1):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO repositories (id, node_id, full_name, status, attempts) "
                        "VALUES (:id, :node, :name, 'DISCOVERED', 0)"
                    ),
                    {"id": repo_id, "node": f"R_{repo_id}", "name": f"org/repo{repo_id}"},
                )
            time.sleep(behaviour.get("delay", 0.05))
    elif stage == "select":
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE repositories SET status = CASE WHEN id % 2 = 1 THEN 'SELECTED' "
                    "ELSE 'REJECTED' END WHERE status = 'DISCOVERED'"
                )
            )
    elif stage == "mine":
        wanted = behaviour.get("wait_for_signals")
        if wanted:
            received = []
            signal.signal(signal.SIGINT, lambda *_: received.append(1))
            (workdir / "mine.started").touch()
            deadline = time.monotonic() + 20
            # Like the real miner: keep working after the first signal, stop after `wanted`.
            while len(received) < wanted and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.3)  # a duplicated forward would arrive here
            (workdir / "mine.signals").write_text(str(len(received)))
            return 130
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE repositories SET status = 'MINED' WHERE status = 'SELECTED'")
            )
    print(f"fake {stage} finished", flush=True)
    return 0


def _report_progress(engine, stage: str) -> None:
    """What track_run saves for a real stage: a RUNNING run whose progress carries the pid."""
    now = datetime.now(UTC)
    progress = {
        "stage": stage.upper(),
        "pid": os.getpid(),
        "host": "test",
        "started_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "phase": f"fake {stage} working",
        "waiting": None,
        "counters": {"repositories": {"done": 1, "total": 2}},
        "workers": [],
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pipeline_runs (stage, status, code_version, study_config_hash, "
                "study_config, params, stats, progress, heartbeat_at) VALUES (:stage, 'RUNNING', "
                "'x', 'h', '{}', '{}', '{}', CAST(:progress AS JSONB), :now)"
            ),
            {"stage": stage.upper(), "progress": json.dumps(progress), "now": now},
        )


if __name__ == "__main__":
    sys.exit(main())
