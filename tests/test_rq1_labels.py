"""RQ1 label creation: prompts, answer validation, propose and consolidate (no network)."""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from agm_miner.config import Rq1Config, Settings
from agm_miner.rq1.backends import PromptRequest
from agm_miner.rq1.codebook import Codebook, load_codebook
from agm_miner.rq1.llm import LLMClient, LLMRequest, LLMRequestError, LLMResult
from agm_miner.rq1.prompts import (
    PROPOSE_SYSTEM,
    InvalidAnswer,
    LabelGroup,
    normalise_label,
    parse_alignment,
    parse_induction,
    parse_mapping,
    parse_proposals,
    render_alignment,
    render_labels,
    render_mapping,
    render_sections,
    short_ids,
    with_alignment,
)
from agm_miner.rq1.stages import (
    IncompleteProposalsError,
    SampleMismatchError,
    consolidate_labels,
    label_groups,
    phase_dir,
    propose_labels,
    sample_sections,
)
from tests.test_rq1_backends import FakeResponse, error, openrouter, prompt
from tests.test_rq1_foundations import gold_dir

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SEED = PROJECT_ROOT / "codebooks" / "seed_agent_readmes.yaml"


# --------------------------------------------------------------------------- prompts


def section(key: str, **overrides: Any) -> dict[str, Any]:
    return {
        "section_key": key,
        "full_name": "org/a",
        "path": "AGENTS.md",
        "tool": "AGENTS",
        "kind": "INSTRUCTION",
        "heading_path": ["Guide", "Testing"],
        "text": "  Run pytest before pushing.\n",
    } | overrides


def test_render_sections_is_deterministic_and_uses_short_ids() -> None:
    ids = short_ids(["k1", "k2"])
    assert ids == {"s1": "k1", "s2": "k2"}
    sections = [section("k2", heading_path=[]), section("k1")]
    text = render_sections(sections, ids)
    assert text == render_sections(list(reversed(sections)), ids)
    assert text.index('<section id="s1">') < text.index('<section id="s2">')
    assert "heading path: Guide > Testing" in text and "heading path: (no heading)" in text
    assert "text:\nRun pytest before pushing.\n</section>" in text
    assert "k1" not in text  # the model never sees the long keys
    assert "between 1 and 3 labels" in PROPOSE_SYSTEM
    # The prompt cites no example label (they steered the pilot's vocabulary).
    assert '"' not in PROPOSE_SYSTEM.split("Answer only")[0]


def answer_for(ids: dict[str, str], **overrides: Any) -> str:
    entries = [
        {
            "id": short_id,
            "is_instruction": True,
            "labels": [{"name": "Test commands", "definition": "How to run the tests."}],
        }
        for short_id in ids
    ]
    for short_id, entry in overrides.items():
        entries = [e for e in entries if e["id"] != short_id]
        if entry is not None:
            entries.append(entry)
    return json.dumps({"sections": entries})


def test_parse_proposals() -> None:
    ids = short_ids(["k1", "k2"])
    proposals = parse_proposals(answer_for(ids), ids)
    assert [p.section_key for p in proposals] == ["k1", "k2"]
    assert proposals[0].labels == (("Test commands", "How to run the tests."),)

    many = {
        "id": "s2",
        "is_instruction": False,
        "labels": [{"name": f"l{i}", "definition": "d"} for i in range(5)],
    }
    assert len(parse_proposals(answer_for(ids, s2=many), ids)[1].labels) == 3

    no_label = {"id": "s2", "is_instruction": True, "labels": [{"name": "", "definition": "d"}]}
    for text, message in [
        (answer_for(ids, s2=None), "without an answer: s2"),
        (answer_for(ids, s2=no_label), "no named, defined label"),
        (answer_for(ids, s2={"id": "s9", "is_instruction": True, "labels": []}), "unknown"),
        (
            '{"sections": [{"id": "s1", "is_instruction": true, "labels": '
            '[{"name": "a", "definition": "b"}]}, {"id": "s1"}]}',
            "twice",
        ),
        ("not json", "not JSON"),
        ("", "empty"),
        ('{"other": 1}', 'no "sections"'),
    ]:
        with pytest.raises(InvalidAnswer, match=message):
            parse_proposals(text, ids)


def test_normalise_label() -> None:
    assert normalise_label("  Code-Style_rules. ") == "code style rules"
    assert normalise_label("Git / Workflow:") == "git workflow"


CATEGORY = {
    "id": "testing",
    "name": "Testing",
    "group": "Build",
    "definition": "How to run and write tests.",
    "include": "Test commands and conventions.",
    "exclude": "CI pipelines (devops).",
    "examples": ["test commands"],
}


def induced(*categories: dict[str, Any]) -> Codebook:
    return parse_induction(
        json.dumps({"categories": list(categories or [CATEGORY])}),
        name="rq1",
        version="draft",
        source="test",
    )


def test_parse_induction() -> None:
    codebook = induced()
    assert codebook.ids == ["testing"] and codebook.categories[0].literature is None
    for bad, message in [
        ({"categories": []}, "no categories"),
        ({"categories": [{**CATEGORY, "id": "other"}]}, "invalid codebook"),
        ({"mapping": []}, 'no "categories"'),
    ]:
        with pytest.raises(InvalidAnswer, match=message):
            parse_induction(json.dumps(bad), name="rq1", version="d", source="t")


def test_induction_prompt_does_not_show_the_reference() -> None:
    text = render_labels([LabelGroup("test commands", 7, 0.86, ("How to run tests.",))])
    assert text.endswith("- test commands (n=7, instruction share 0.86): How to run tests.")
    assert "Agent READMEs" not in text and "Reference" not in text


def test_alignment() -> None:
    reference = load_codebook(SEED)
    persona = {**CATEGORY, "id": "agent_persona", "name": "Persona"}
    codebook = induced(CATEGORY, persona)
    text = render_alignment(codebook, reference)
    assert "Induced taxonomy:" in text and "- id: build_and_run" in text

    answer = {
        "alignment": [
            {"category": "testing", "reference": "testing"},
            {"category": "agent_persona", "reference": ""},
        ]
    }
    alignment = parse_alignment(json.dumps(answer), codebook, reference)
    assert alignment == {"testing": "testing", "agent_persona": ""}
    aligned = with_alignment(codebook, alignment, reference)
    assert [c.literature for c in aligned.categories] == ["Agent READMEs: Testing", None]

    for bad, message in [
        ({"alignment": [{"category": "testing", "reference": "testing"}]}, "without an alignment"),
        ({"alignment": [{"category": "x", "reference": ""}]}, "unknown induced"),
        ({"alignment": [{"category": "testing", "reference": "nope"}]}, "unknown reference"),
    ]:
        with pytest.raises(InvalidAnswer, match=message):
            parse_alignment(json.dumps(bad), codebook, reference)


def test_mapping() -> None:
    codebook = induced()
    groups = [LabelGroup("test commands", 3, 1.0, ("d",)), LabelGroup("persona", 1, 1.0, ("d",))]
    assert "- id: testing" in render_mapping(codebook, groups)
    answer = {
        "mapping": [
            {"label": "Test Commands", "category": "testing"},
            {"label": "persona", "category": "other"},
        ]
    }
    labels = ["test commands", "persona"]
    assert parse_mapping(json.dumps(answer), labels, codebook) == {
        "test commands": "testing",
        "persona": "other",
    }
    for bad, message in [
        ({"mapping": []}, "2 labels without a category"),  # what the pilot's model answered
        ({"mapping": [{"label": "x", "category": "testing"}]}, "unknown label"),
        ({"mapping": [{"label": "persona", "category": "nope"}]}, "unknown category"),
    ]:
        with pytest.raises(InvalidAnswer, match=message):
            parse_mapping(json.dumps(bad), labels, codebook)


# --------------------------------------------------------------------------- fake backend

_ID_RE = re.compile(
    r'<section id="(s\d+)">\nrepository: [^\n]*\nfile: [^\n]*\nheading path: ([^\n]*)'
)


class FakeBackend:
    """Answers like a model would: one label per section, from its last heading. ``broken``
    request keys get an answer missing a section (outside the requested format)."""

    provider, model = "openrouter", "fake-model"  # as configured in setup()

    def __init__(self, broken: set[str] = frozenset()) -> None:
        self.broken = set(broken)
        self.asked: list[str] = []

    def observe_waits(self, observer) -> None:
        pass

    def answer(self, request: PromptRequest) -> LLMResult:
        self.asked.append(request.custom_id)
        if request.schema_name == "codebook":
            text = json.dumps({"categories": [CATEGORY]})
        elif request.schema_name == "alignment":
            text = json.dumps({"alignment": [{"category": "testing", "reference": "testing"}]})
        elif request.schema_name == "mapping":
            labels = re.findall(r"^- (.+?) \(n=", request.user, re.MULTILINE)
            text = json.dumps(
                {"mapping": [{"label": label, "category": "testing"} for label in labels]}
            )
        else:
            entries = [
                {
                    "id": short_id,
                    "is_instruction": True,
                    "labels": [{"name": heading.split(" > ")[-1], "definition": "d"}],
                }
                for short_id, heading in _ID_RE.findall(request.user)
            ]
            if request.custom_id in self.broken:
                entries = entries[1:]
            text = json.dumps({"sections": entries})
        return LLMResult(request.custom_id, "succeeded", text=text, usage={"input_tokens": 1})

    def complete(self, request: PromptRequest) -> LLMResult:
        return self.answer(request)

    def execute(self, prompts, cache, state, progress=None):
        results = {}
        for request in prompts:
            cached = cache.get(request.custom_id)
            if cached is None:
                cached = self.answer(request)
                cache.put(cached)
            results[request.custom_id] = cached
        return results


def setup(tmp_path: Path, **config: Any) -> tuple[Path, Path, Rq1Config]:
    gold = gold_dir(tmp_path)
    output = tmp_path / "rq1" / gold.name
    rq1 = Rq1Config(
        sample_size=30,
        per_repo_cap=10,
        propose_chunk_sections=10,
        provider="openrouter",
        propose_model="fake-model",
        **config,
    )
    sample_sections(gold, output, rq1)
    return gold, output, rq1


# --------------------------------------------------------------------------- propose / consolidate


def test_propose_writes_proposals_and_reuses_answers(tmp_path: Path) -> None:
    gold, output, config = setup(tmp_path)
    backend = FakeBackend()
    summary = propose_labels(gold, output, config, backend)
    assert summary["requests"] == 3 and summary["coverage"] == 1.0
    assert summary["failed"] == {} and summary["given_up"] == []
    proposals = pl.read_parquet(Path(summary["output"]))
    assert proposals.height == 30 and set(proposals["label_norm"]) == {"testing"}
    manifest = json.loads((Path(summary["output"]).parent / "proposals.json").read_text())
    assert manifest["prompt_version"] == "propose-v2" and manifest["sample_hash"]

    again = FakeBackend()
    propose_labels(gold, output, config, again)
    assert again.asked == []  # every answer came from the cache


def test_answers_outside_the_format_are_retried_then_given_up(tmp_path: Path) -> None:
    gold, output, config = setup(tmp_path, max_attempts=2)
    first = FakeBackend()
    propose_labels(gold, output, config, first)
    broken = first.asked[0]

    summary = propose_labels(gold, output, config, FakeBackend(broken={broken}))
    assert summary["coverage"] == 1.0  # the broken answer was not the cached one

    # A request whose answer is never usable: retried until max_attempts, then left out.
    import shutil

    shutil.rmtree(output / "cache")
    for _ in range(2):
        summary = propose_labels(gold, output, config, FakeBackend(broken={broken}))
        assert "without an answer" in summary["failed"][broken]
    last = FakeBackend(broken={broken})
    summary = propose_labels(gold, output, config, last)
    assert summary["given_up"] == [broken] and broken not in last.asked
    assert summary["coverage"] == round(20 / 30, 4) and summary["sections_with_proposals"] == 20


def test_consolidate_induces_aligns_and_maps(tmp_path: Path) -> None:
    gold, output, config = setup(tmp_path, consolidate_model="fake-model")
    propose_labels(gold, output, config, FakeBackend())
    backend = FakeBackend()
    summary = consolidate_labels(gold, output, config, backend, seed_codebook=SEED)
    draft = load_codebook(Path(summary["output"]))
    assert draft.name == "rq1" and draft.version.startswith("draft-") and draft.ids == ["testing"]
    assert draft.categories[0].literature == "Agent READMEs: Testing"
    assert "fake-model" in draft.source and "propose-v2" in draft.source
    assert summary["categories"] == 1 and summary["new_categories"] == 0
    assert summary["mapped_labels"] == summary["labels"] == 1 and summary["other_share"] == 0.0
    assert len(backend.asked) == 3  # induce, align, one mapping chunk
    label_map = pl.read_parquet(Path(summary["output"]).parent / "label_map.parquet")
    assert label_map.to_dicts() == [
        {"label": "testing", "category": "testing", "sections": 30, "instruction_share": 1.0}
    ]

    # Another day: every step comes from the cache (the draft version does not use the date).
    again = FakeBackend()
    assert consolidate_labels(gold, output, config, again, seed_codebook=SEED)["categories"] == 1
    assert again.asked == []


def test_consolidate_refuses_stale_or_incomplete_proposals(tmp_path: Path) -> None:
    gold, output, config = setup(tmp_path, max_attempts=1, consolidate_model="fake-model")
    first = FakeBackend()
    propose_labels(gold, output, config, first)
    import shutil

    shutil.rmtree(output / "cache")
    propose_labels(gold, output, config, FakeBackend(broken={first.asked[0]}))
    with pytest.raises(IncompleteProposalsError, match="67%"):
        consolidate_labels(gold, output, config, FakeBackend(), seed_codebook=SEED)
    consolidate_labels(gold, output, config, FakeBackend(), seed_codebook=SEED, allow_partial=True)

    # A different sample (here: a different size) is refused unless forced.
    bigger = config.model_copy(update={"sample_size": 40})
    with pytest.raises(SampleMismatchError, match="--force"):
        sample_sections(gold, output, bigger)
    sample_sections(gold, output, bigger, force=True)
    with pytest.raises(SampleMismatchError, match="another sample"):
        consolidate_labels(gold, output, config, FakeBackend(), seed_codebook=SEED)


def test_label_groups_order_and_definitions() -> None:
    proposals = pl.DataFrame(
        {
            "section_key": ["a", "b", "c", "c"],
            "label_norm": ["x", "x", "y", "x"],
            "definition": ["d1", "d2", "d3", "d1"],
            "is_instruction": [True, False, True, True],
        }
    )
    groups = label_groups(proposals)
    assert [(g.label, g.count) for g in groups] == [("x", 3), ("y", 1)]
    assert groups[0].definitions == ("d1", "d2") and groups[0].instruction_share == 0.67


def test_phase_dir_slugs_model_ids(tmp_path: Path) -> None:
    assert phase_dir(tmp_path, "propose", "openrouter", "nvidia/x-y:free") == (
        tmp_path / "propose" / "openrouter" / "nvidia_x-y_free"
    )


# --------------------------------------------------------------------------- provider details


def test_claude_single_requests_use_refusal_fallbacks() -> None:
    calls: list[tuple[str, dict]] = []

    @contextmanager
    def streamer(kind: str, **kwargs: Any):
        calls.append((kind, kwargs))
        final = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="{}")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            model="claude-opus-4-8",
        )
        yield SimpleNamespace(get_final_message=lambda: final)

    sdk = SimpleNamespace(
        messages=SimpleNamespace(stream=lambda **k: streamer("plain", **k)),
        beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **k: streamer("beta", **k))),
    )
    client = LLMClient(Settings(DATABASE_URL="postgresql://unused"), client=sdk)
    result = client.complete(LLMRequest("k", {"model": "claude-opus-5-5", "messages": []}))
    assert calls[0][0] == "beta" and calls[0][1]["fallbacks"] == "default"
    assert calls[0][1]["betas"] == ["server-side-fallback-2026-07-01"]
    assert result.served_by == "claude-opus-4-8"  # the fallback model answered

    client.complete(LLMRequest("k", {"model": "claude-haiku-5-5", "messages": []}))
    assert calls[1][0] == "plain" and "fallbacks" not in calls[1][1]


def test_openrouter_temporary_402_is_retried_and_404_suggests_other_models() -> None:
    backend, session, clock = openrouter(
        error(402, "in_flight_budget_exhausted", {"Retry-After": "4"}),
        FakeResponse(
            200,
            {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": {}},
        ),
    )
    assert backend.complete(prompt()).ok and clock.sleeps[0] == 4

    backend, _, _ = openrouter(error(402, "Insufficient credits"))
    with pytest.raises(LLMRequestError, match="402"):
        backend.complete(prompt())

    backend, _, _ = openrouter(error(404, "Provider returned error: Function not found"))
    with pytest.raises(LLMRequestError, match="agm rq1 models"):
        backend.complete(prompt())
