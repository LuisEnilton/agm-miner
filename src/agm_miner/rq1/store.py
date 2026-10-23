"""On-disk state that makes the LLM phases resumable and never pay twice for an answer.

* :class:`ResponseCache`: one JSON file per successful answer, named by its request key. A request
  key covers everything that determines the answer (phase, model, prompt version, codebook and
  the unit), so changing any of them asks again while re-runs reuse what is already answered.
* :class:`BatchState`: the Message Batches submitted and not yet collected. An interrupted command
  finds them on the next run and waits for those batches instead of submitting the work again.

Files are written to a temporary name and renamed, so an interruption never leaves a half-written
entry.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from agm_miner.rq1.llm import LLMResult


def request_key(*parts: str) -> str:
    """Stable 64-character key (valid as a batch ``custom_id``)."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


class ResponseCache:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, key: str) -> Path:
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
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def pending(self) -> dict[str, list[str]]:
        """Batch id -> custom ids of the batches submitted and not yet collected."""
        return self._load()

    def add(self, batch_id: str, custom_ids: list[str]) -> None:
        state = self._load()
        state[batch_id] = custom_ids
        _write_json(self.path, state)

    def remove(self, batch_id: str) -> None:
        state = self._load()
        state.pop(batch_id, None)
        _write_json(self.path, state)
