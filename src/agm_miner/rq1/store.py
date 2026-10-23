"""On-disk state that makes the LLM phases resumable and never pay twice for an answer.

* :class:`ResponseCache`: one JSON file per successful answer, named by its request key. A request
  key covers everything that determines the answer (phase, model, prompt version, codebook and
  the unit), so changing any of them asks again while re-runs reuse what is already answered.
* :class:`BatchState`: the Message Batches submitted and not yet collected. An interrupted command
  finds them on the next run and waits for those batches instead of submitting the work again.

Files are written to a temporary name and renamed, so an interruption never leaves a half-written
entry. :func:`run_lock` keeps a second command from working on the same RQ1 directory at the same
time (it would submit the same requests again and pay twice).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agm_miner.rq1.llm import LLMResult

_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class Rq1BusyError(RuntimeError):
    """Another command is already working on this RQ1 directory."""


@contextmanager
def run_lock(directory: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``directory`` for the duration of a command."""
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Rq1BusyError(
                f"Another `agm rq1` command is already working on {directory}; wait for it to "
                "finish (running both would send the same requests twice)."
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def request_key(*parts: str) -> str:
    """Stable 64-character key (valid as a batch ``custom_id``)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _checked(key: str) -> str:
    # Keys become file names: accept only what request_key produces.
    if not _KEY_RE.match(key):
        raise ValueError(f"not a request key: {key!r}")
    return key


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class ResponseCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
        _checked(key)
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> LLMResult | None:
        path = self._path(key)
        if not path.exists():
            return None
        return LLMResult.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def put(self, result: LLMResult) -> None:
        """Only successful answers are cached: failures are asked again on the next run."""
        if result.ok:
            _write_json(self._path(result.custom_id), result.as_dict())


class BatchState:
    """Batches submitted and not yet collected. Every change is a locked read-modify-write, so
    two processes never drop each other's entries."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def pending(self) -> dict[str, list[str]]:
        """Batch id -> custom ids of the batches submitted and not yet collected."""
        return self._load()

    def _update(self, change: Callable[[dict[str, list[str]]], None]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_name(self.path.name + ".lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                state = self._load()
                change(state)
                _write_json(self.path, state)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def add(self, batch_id: str, custom_ids: list[str]) -> None:
        self._update(lambda state: state.__setitem__(batch_id, custom_ids))

    def remove(self, batch_id: str) -> None:
        self._update(lambda state: state.pop(batch_id, None))
