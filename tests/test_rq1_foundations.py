"""RQ1 foundations: units, sampling, codebook, LLM client, cache/state and batch execution."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from agm_miner.analysis.datasets import ANALYSIS_MANIFEST
from agm_miner.cli import app
from agm_miner.config import Settings
from agm_miner.progress import Progress
from agm_miner.rq1.codebook import Codebook, dump_codebook, load_codebook
from agm_miner.rq1.execution import execute_batched, usage_totals
from agm_miner.rq1.llm import (
    BatchProgress,
    LLMAuthError,
    LLMClient,
    LLMRequest,
    LLMResult,
    result_from_message,
)
from agm_miner.rq1.sampling import allocate, length_bucket, stratified_sample
from agm_miner.rq1.store import BatchState, ResponseCache, request_key
from agm_miner.rq1.units import artifact_chunks, build_units, section_key

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def sections(rows: list[dict]) -> pl.DataFrame:
    defaults = {
        "version_id": 1,
        "artifact_id": 1,
        "repo_id": 1,
        "position": 0,
        "level": 2,
        "heading": "Testing",
        "heading_path": ["Guide", "Testing"],
        "h1": "Guide",
        "h2": "Testing",
        "h3": None,
        "text": "Run pytest.",
        "words": 2,
        "full_name": "org/a",
        "path": "AGENTS.md",
        "tool": "AGENTS",
        "kind": "INSTRUCTION",
        "scope": "ROOT",
        "skill_name": None,
    }
    return pl.DataFrame([defaults | row for row in rows])


# --------------------------------------------------------------------------- units


def test_section_key_depends_on_heading_path_and_text_only() -> None:
    key = section_key(["Guide", "Testing"], "Run pytest.")
    assert key == section_key(("Guide", "Testing"), "Run pytest.") and len(key) == 64
    assert key != section_key(["Guide", "Build"], "Run pytest.")
    assert key != section_key(["Guide", "Testing"], "Run tox.")


def test_units_skip_heading_only_sections_and_chunk_by_file() -> None:
    units = build_units(
        sections(
            [
                {"artifact_id": 2, "position": 1, "text": "b"},
                {"artifact_id": 2, "position": 0, "text": "a"},
                {"artifact_id": 2, "position": 2, "text": "  \n"},  # heading only
                {"artifact_id": 1, "position": 0, "text": "c"},
            ]
        )
    )
    assert units.height == 3
    chunks = [[row["text"] for row in chunk] for chunk in artifact_chunks(units, size=1)]
    assert chunks == [["c"], ["a"], ["b"]]
    assert [len(c) for c in artifact_chunks(units, size=30)] == [1, 2]


# --------------------------------------------------------------------------- sampling


def test_length_bucket_and_allocation() -> None:
    assert [length_bucket(w, [50, 150]) for w in (0, 49, 50, 149, 150)] == [0, 0, 1, 1, 2]
    assert allocate({"a": 90, "b": 9, "c": 1}, 10) == {"a": 8, "b": 1, "c": 1}
    assert sum(allocate({"a": 5, "b": 5, "c": 5}, 4).values()) == 4
    assert allocate({"a": 2, "b": 1}, 10) == {"a": 2, "b": 1}  # never more than available


def make_units(count: int, repos: int) -> pl.DataFrame:
    return build_units(
        sections(
            [
                {
                    "repo_id": i % repos,
                    "artifact_id": i,
                    "text": f"section {i}",
                    "words": (i * 7) % 300,
                    "tool": "CLAUDE" if i % 3 else "AGENTS",
                }
                for i in range(count)
            ]
        )
    )


def test_stratified_sample_is_deterministic_capped_and_excludes() -> None:
    units = make_units(300, repos=30)
    options = {"per_repo_cap": 3, "seed": 7, "length_bins": [50, 150]}
    first = stratified_sample(units, 60, **options)
    assert first.height == 60
    assert first.equals(stratified_sample(units, 60, **options))
    assert not first.equals(stratified_sample(units, 60, **options | {"seed": 8}))
    assert first.group_by("repo_id").len()["len"].max() <= 3
    assert set(first["stratum"]) == set(
        stratified_sample(units, 300, **options | {"per_repo_cap": 300})["stratum"]
    )

    fresh = stratified_sample(units, 60, **options, exclude_keys=first["section_key"])
    assert not set(fresh["section_key"]) & set(first["section_key"])


def test_sample_counts_identical_sections_once() -> None:
    units = build_units(sections([{"artifact_id": i, "repo_id": i} for i in range(5)]))
    assert stratified_sample(units, 10, per_repo_cap=5, seed=1, length_bins=[50]).height == 1


# --------------------------------------------------------------------------- codebook


def test_seed_codebook_and_validation(tmp_path: Path) -> None:
    seed = load_codebook(PROJECT_ROOT / "codebooks" / "seed_agent_readmes.yaml")
    assert len(seed.ids) == 16 and "testing" in seed.ids and seed.label == "agent_readmes@2025"

    dump_codebook(seed, tmp_path / "copy.yaml")
    assert load_codebook(tmp_path / "copy.yaml") == seed

    changed = seed.model_copy(
        update={"categories": [seed.categories[0].model_copy(update={"definition": "x"})]}
    )
    assert changed.fingerprint() != seed.fingerprint()

    category = {"name": "X", "definition": "x"}
    with pytest.raises(ValidationError, match="reserved"):
        Codebook(name="c", version="1", categories=[{"id": "other", **category}])
    with pytest.raises(ValidationError, match="duplicated"):
        Codebook(name="c", version="1", categories=[{"id": "a", **category}] * 2)
    with pytest.raises(ValidationError):
        Codebook(name="c", version="1", categories=[{"id": "Bad Id", **category}])


# --------------------------------------------------------------------------- LLM client


def message(text: str, stop_reason: str = "end_turn", **usage: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(**({"input_tokens": 10, "output_tokens": 5} | usage)),
    )


def test_result_from_message() -> None:
    result = result_from_message("id", message('{"a": 1}', cache_read_input_tokens=7))
    assert result.ok and result.text == '{"a": 1}'
    assert result.usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 0,
    }
    assert result_from_message("id", message("", "refusal")).status == "refused"
    assert result_from_message("id", message("{", "max_tokens")).status == "truncated"


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list] = []
        self.polls = 0

    def create(self, requests):
        self.created.append(requests)
        return SimpleNamespace(id=f"batch_{len(self.created)}")

    def retrieve(self, batch_id):
        self.polls += 1
        ended = self.polls >= 2
        counts = SimpleNamespace(processing=0 if ended else 2, succeeded=1, errored=1)
        return SimpleNamespace(
            processing_status="ended" if ended else "in_progress", request_counts=counts
        )

    def results(self, batch_id):
        yield SimpleNamespace(custom_id="b", result=SimpleNamespace(type="expired"))
        yield SimpleNamespace(
            custom_id="a", result=SimpleNamespace(type="succeeded", message=message("ok"))
        )
        yield SimpleNamespace(
            custom_id="c",
            result=SimpleNamespace(type="errored", error="invalid_request: bad schema"),
        )


def fake_sdk() -> SimpleNamespace:
    return SimpleNamespace(messages=SimpleNamespace(batches=FakeBatches()))


def test_llm_client_batches_and_waits() -> None:
    sleeps: list[float] = []
    waits: list[str] = []
    client = LLMClient(
        Settings(DATABASE_URL="postgresql://unused"),
        client=fake_sdk(),
        sleep=sleeps.append,
        poll_seconds=5,
    )

    @contextmanager
    def observe(reason: str, seconds: float):
        waits.append(reason)
        yield

    client.observe_waits(observe)
    batch_id = client.submit_batch(
        [LLMRequest("a", {"model": "claude-sonnet-5-5", "max_tokens": 10, "messages": []})]
    )
    assert batch_id == "batch_1"
    seen: list[BatchProgress] = []
    client.wait_for_batch(batch_id, seen.append)
    assert [p.ended for p in seen] == [False, True] and sleeps == [5]
    assert waits == ["Claude batch batch_1: 2 requests still processing"]
    statuses = {r.custom_id: r.status for r in client.batch_results(batch_id)}
    assert statuses == {"a": "succeeded", "b": "expired", "c": "errored"}


def test_missing_credentials_raise_auth_error() -> None:
    class NoAuth:
        def create(self, **_):
            raise TypeError("Could not resolve authentication method. Expected one of api_key")

    client = LLMClient(
        Settings(DATABASE_URL="postgresql://unused"),
        client=SimpleNamespace(messages=SimpleNamespace(batches=NoAuth())),
    )
    with pytest.raises(LLMAuthError, match="ANTHROPIC_API_KEY"):
        client.submit_batch([])


# --------------------------------------------------------------------------- cache and state


def test_cache_keeps_only_successful_answers(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    key = request_key("classify", "claude-sonnet-5-5", "v1", "abc")
    assert len(key) == 64 and key == request_key("classify", "claude-sonnet-5-5", "v1", "abc")
    cache.put(LLMResult(key, "succeeded", text="{}", usage={"input_tokens": 3}))
    cache.put(LLMResult("f" * 64, "refused"))
    assert cache.get(key).text == "{}" and cache.get("f" * 64) is None

    state = BatchState(tmp_path / "batches.json")
    state.add("batch_1", ["a"])
    state.add("batch_2", ["b"])
    state.remove("batch_1")
    assert state.pending() == {"batch_2": ["b"]}


# --------------------------------------------------------------------------- execution


class FakeLLM:
    """Answers every request; ``fail`` ids come back errored. Records what was submitted."""

    def __init__(self, fail: set[str] = frozenset()) -> None:
        self.fail = fail
        self.batches: dict[str, list[str]] = {}

    def submit_batch(self, requests):
        batch_id = f"batch_{len(self.batches) + 1}"
        self.batches[batch_id] = [request.custom_id for request in requests]
        return batch_id

    def wait_for_batch(self, batch_id, on_progress):
        on_progress(BatchProgress(True, {"processing": 0, "succeeded": 1}))

    def batch_results(self, batch_id):
        for custom_id in reversed(self.batches[batch_id]):  # results come in any order
            if custom_id in self.fail:
                yield LLMResult(custom_id, "errored", error="overloaded")
            else:
                yield LLMResult(custom_id, "succeeded", text=custom_id, usage={"input_tokens": 2})


def requests(*ids: str) -> list[LLMRequest]:
    return [LLMRequest(custom_id, {}) for custom_id in ids]


def test_execute_batched_caches_chunks_and_retries_failures(tmp_path: Path) -> None:
    cache, state = ResponseCache(tmp_path / "cache"), BatchState(tmp_path / "state.json")
    llm = FakeLLM(fail={"c"})
    progress = Progress()
    results = execute_batched(
        llm, requests("a", "b", "c"), cache, state, progress, max_batch_requests=2
    )
    assert {k: r.status for k, r in results.items()} == {
        "a": "succeeded",
        "b": "succeeded",
        "c": "errored",
    }
    assert list(llm.batches.values()) == [["a", "b"], ["c"]]
    assert state.pending() == {}
    assert usage_totals(results.values()) == {"input_tokens": 4}
    assert progress.snapshot()["counters"]["requests"] == {"done": 3, "total": 3}

    # A second run only asks what failed.
    retry = FakeLLM()
    results = execute_batched(retry, requests("a", "b", "c"), cache, state)
    assert list(retry.batches.values()) == [["c"]]
    assert all(result.ok for result in results.values())


def test_execute_batched_resumes_an_interrupted_batch(tmp_path: Path) -> None:
    cache, state = ResponseCache(tmp_path / "cache"), BatchState(tmp_path / "state.json")
    llm = FakeLLM()
    # An earlier run submitted a batch and was interrupted before collecting it.
    llm.batches["batch_9"] = ["a", "b"]
    state.add("batch_9", ["a", "b"])
    results = execute_batched(llm, requests("a", "b", "c"), cache, state)
    assert sorted(results) == ["a", "b", "c"]
    # Only "c" was submitted again; "a" and "b" came from the pending batch.
    assert llm.batches == {"batch_9": ["a", "b"], "batch_2": ["c"]}
    assert state.pending() == {}


# --------------------------------------------------------------------------- sample stage


def gold_dir(tmp_path: Path) -> Path:
    gold = tmp_path / "gold" / "20261022T000000Z"
    gold.mkdir(parents=True)
    rows = [
        {"repo_id": i % 8, "artifact_id": i, "text": f"text {i}", "words": i % 200}
        for i in range(80)
    ]
    sections(rows).write_parquet(gold / "rq1_sections.parquet")
    (gold / ANALYSIS_MANIFEST).write_text(json.dumps({"outputs": {}}))
    return gold


def test_rq1_sample_command(tmp_path: Path, monkeypatch) -> None:
    gold = gold_dir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(app, ["rq1", "sample", "--size", "20"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output[result.output.index("{") :])
    assert summary["sampled"] == 20 and summary["units"] == 80
    output = tmp_path / "rq1" / gold.name
    sample = pl.read_parquet(output / "sample.parquet")
    assert sample.height == 20 and sample.group_by("repo_id").len()["len"].max() <= 5
    manifest = json.loads((output / "sample.json").read_text())
    assert manifest["gold_dir"] == str(gold) and manifest["rq1_config"]["sample_size"] == 20
