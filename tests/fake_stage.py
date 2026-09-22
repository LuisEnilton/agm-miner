"""Stand-in for ``agm discover|select|mine`` used by the orchestrator tests.

Run as ``python tests/fake_stage.py <stage> [args...]``; it changes repository statuses in the
test database the way the real stage would. Behaviour is configured through ``FAKE_STAGE``
(JSON), e.g. ``{"select": {"exit": 1}}`` or ``{"mine": {"wait_for_signals": 2}}``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text


def main() -> int:
    stage = sys.argv[1]
    behaviour = json.loads(os.environ.get("FAKE_STAGE", "{}")).get(stage, {})
    workdir = Path(os.environ["FAKE_STAGE_DIR"])
    with (workdir / "calls.log").open("a") as log:
        log.write(" ".join(sys.argv[1:]) + "\n")
    print(f"fake {stage} started", flush=True)
    if "exit" in behaviour:
        return int(behaviour["exit"])

    engine = create_engine(os.environ["DATABASE_URL"])
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


if __name__ == "__main__":
    sys.exit(main())
