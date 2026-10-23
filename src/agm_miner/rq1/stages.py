"""RQ1 stages. Each reads an analysis output (gold) and writes into
``DATA_DIR/rq1/<gold name>/`` with a JSON manifest next to every result.

Layout of that directory::

    sample.parquet, sample.json                  label-creation sample
    propose/<provider>/<model>/proposals.*        free labels per sampled section
    consolidate/<provider>/<model>/               codebook.draft.yaml, label_map.parquet
    cache/, batches.json, failures.json           LLM answers, pending batches, failed requests
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import polars as pl

from agm_miner.analysis.datasets import ANALYSIS_MANIFEST
from agm_miner.config import Rq1Config
from agm_miner.progress import Progress
from agm_miner.rq1.backends import LLMBackend, PromptRequest
from agm_miner.rq1.codebook import OTHER, dump_codebook, load_codebook
from agm_miner.rq1.execution import usage_totals
from agm_miner.rq1.llm import LLMResult
from agm_miner.rq1.prompts import (
    ALIGN_SCHEMA,
    ALIGN_SYSTEM,
    ALIGN_VERSION,
    INDUCE_SCHEMA,
    INDUCE_VERSION,
    MAP_SCHEMA,
    MAP_SYSTEM,
    MAP_VERSION,
    PROPOSE_SCHEMA,
    PROPOSE_SYSTEM,
    PROPOSE_VERSION,
    InvalidAnswer,
    LabelGroup,
    induce_system,
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
from agm_miner.rq1.sampling import stratified_sample
from agm_miner.rq1.store import BatchState, FailureLog, ResponseCache, request_key, run_lock
from agm_miner.rq1.units import load_units
from agm_miner.runs import code_version

SAMPLE = "sample.parquet"
SAMPLE_MANIFEST = "sample.json"
PROPOSALS = "proposals.parquet"
PROPOSALS_MANIFEST = "proposals.json"
DRAFT_CODEBOOK = "codebook.draft.yaml"
LABEL_MAP = "label_map.parquet"
CONSOLIDATE_MANIFEST = "consolidate.json"

T = TypeVar("T")


class SampleMismatchError(RuntimeError):
    """Results made for one sample would be mixed with another sample."""


class IncompleteProposalsError(RuntimeError):
    """Too few sampled sections have proposals to consolidate them."""


def _write_parquet(frame: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    frame.write_parquet(temporary)
    temporary.replace(path)


def _write_manifest(path: Path, gold_dir: Path, config: Rq1Config, **details: Any) -> None:
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "code_version": code_version(),
        "gold_dir": str(gold_dir),
        "analysis_manifest": json.loads((gold_dir / ANALYSIS_MANIFEST).read_text("utf-8")),
        "rq1_config": config.model_dump(mode="json"),
        **details,
    }
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def sample_hash(sample: pl.DataFrame) -> str:
    """Identity of a sample: the set of its section keys."""
    keys = "\n".join(sorted(sample["section_key"].to_list()))
    return hashlib.sha256(keys.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def model_slug(model: str) -> str:
    """A model id usable as a folder name (``nvidia/x:free`` -> ``nvidia_x_free``)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


def phase_dir(output_dir: Path, phase: str, provider: str, model: str) -> Path:
    return output_dir / phase / provider / model_slug(model)


def sample_sections(
    gold_dir: Path, output_dir: Path, config: Rq1Config, *, force: bool = False
) -> dict[str, Any]:
    """Stratified label-creation sample of the sections with text (``sample.parquet``).

    Replacing a sample that already has proposals requires ``force``: the proposals would no
    longer describe the sample.
    """
    with run_lock(output_dir):
        return _sample_sections(gold_dir, output_dir, config, force)


def _sample_sections(
    gold_dir: Path, output_dir: Path, config: Rq1Config, force: bool
) -> dict[str, Any]:
    units = load_units(gold_dir)
    sample = stratified_sample(
        units,
        config.sample_size,
        per_repo_cap=config.per_repo_cap,
        seed=config.seed,
        length_bins=config.length_bins,
    )
    identity = sample_hash(sample)
    previous = output_dir / SAMPLE_MANIFEST
    if (
        not force
        and previous.exists()
        and _read_json(previous).get("sample_hash") != identity
        and (output_dir / "propose").exists()
    ):
        raise SampleMismatchError(
            "This would replace a sample that already has proposals (the configuration or the "
            "data changed). Use --force to replace it; then run `agm rq1 propose` again."
        )
    _write_parquet(sample, output_dir / SAMPLE)
    summary = {
        "sample_hash": identity,
        "units": units.height,
        "distinct_units": units["section_key"].n_unique(),
        "sampled": sample.height,
        "repositories": sample["repo_id"].n_unique(),
        "strata": dict(sorted(Counter(sample["stratum"].to_list()).items())),
    }
    _write_manifest(previous, gold_dir, config, **summary)
    return {"output": str(output_dir / SAMPLE), **summary}


def _load_sample(output_dir: Path) -> tuple[pl.DataFrame, str]:
    path = output_dir / SAMPLE
    if not path.exists():
        raise FileNotFoundError(f"No sample in {output_dir}: run `agm rq1 sample` first")
    sample = pl.read_parquet(path)
    return sample, sample_hash(sample)


# --------------------------------------------------------------------------- LLM requests


def _settle(
    key: str,
    result: LLMResult | None,
    validate: Callable[[LLMResult], T],
    cache: ResponseCache,
    failures: FailureLog,
) -> T | None:
    """Validated answer of one request, or None after recording why it failed."""
    if result is None or not result.ok:
        error = "no answer" if result is None else (result.error or result.status)
        failures.record(key, error)
        return None
    try:
        answer = validate(result)
    except InvalidAnswer as exc:
        cache.delete(key)  # ask again next run instead of reusing an answer in the wrong format
        failures.record(key, f"invalid answer: {exc}")
        return None
    failures.clear(key)
    return answer


def _run_requests(
    backend: LLMBackend,
    prompts: Sequence[PromptRequest],
    output_dir: Path,
    config: Rq1Config,
    progress: Progress,
    validate: Callable[[PromptRequest, LLMResult], T],
) -> tuple[dict[str, T], dict[str, Any]]:
    """Run ``prompts`` (cached, resumable), validate every answer and report failures."""
    cache = ResponseCache(output_dir / "cache")
    failures = FailureLog(output_dir / "failures.json")
    keys = [prompt.custom_id for prompt in prompts]
    given_up = set(failures.given_up(keys, config.max_attempts))
    to_run = [prompt for prompt in prompts if prompt.custom_id not in given_up]
    backend.observe_waits(progress.waiting)
    results = backend.execute(to_run, cache, BatchState(output_dir / "batches.json"), progress)
    answers: dict[str, T] = {}
    for prompt in to_run:
        answer = _settle(
            prompt.custom_id,
            results.get(prompt.custom_id),
            lambda result, prompt=prompt: validate(prompt, result),
            cache,
            failures,
        )
        if answer is not None:
            answers[prompt.custom_id] = answer
    failed = [key for key in keys if key not in answers]
    report = {
        "requests": len(prompts),
        "answered": len(answers),
        "failed": failures.errors(failed),
        "given_up": sorted(given_up),
        "usage": usage_totals(results.values()),
        "served_by": dict(Counter(r.served_by for r in results.values() if r.served_by)),
    }
    return answers, report


# --------------------------------------------------------------------------- propose


def propose_labels(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Free labels for every sampled section (several sections per request)."""
    with run_lock(output_dir):
        return _propose_labels(gold_dir, output_dir, config, backend, progress or Progress())


def _propose_labels(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    progress: Progress,
) -> dict[str, Any]:
    sample, identity = _load_sample(output_dir)
    rows = sample.sort("section_key").to_dicts()
    size = config.propose_chunk_sections
    prompts: list[PromptRequest] = []
    ids_of: dict[str, dict[str, str]] = {}
    for start in range(0, len(rows), size):
        chunk = rows[start : start + size]
        keys = [row["section_key"] for row in chunk]
        key = request_key("propose", PROPOSE_VERSION, backend.provider, backend.model, *keys)
        ids_of[key] = short_ids(keys)
        prompts.append(
            PromptRequest(
                key,
                PROPOSE_SYSTEM,
                render_sections(chunk, ids_of[key]),
                PROPOSE_SCHEMA,
                config.propose_max_tokens,
                "proposals",
            )
        )
    progress.set_phase(f"proposing labels for {len(rows)} sections with {backend.model}")
    answers, report = _run_requests(
        backend,
        prompts,
        output_dir,
        config,
        progress,
        lambda prompt, result: parse_proposals(result.text, ids_of[prompt.custom_id]),
    )

    context = {row["section_key"]: row for row in rows}
    records = []
    for key, proposals in answers.items():
        for proposal in proposals:
            section = context[proposal.section_key]
            for rank, (name, definition) in enumerate(proposal.labels, start=1):
                records.append(
                    {
                        "section_key": proposal.section_key,
                        "repo_id": section["repo_id"],
                        "full_name": section["full_name"],
                        "path": section["path"],
                        "tool": section["tool"],
                        "kind": section["kind"],
                        "heading_path": section["heading_path"],
                        "is_instruction": proposal.is_instruction,
                        "rank": rank,
                        "label": name,
                        "label_norm": normalise_label(name),
                        "definition": definition,
                        "request_key": key,
                    }
                )
    proposals_frame = pl.DataFrame(records, schema=_PROPOSALS_SCHEMA)
    covered = proposals_frame["section_key"].n_unique()
    destination = phase_dir(output_dir, "propose", backend.provider, backend.model)
    _write_parquet(proposals_frame, destination / PROPOSALS)
    summary = {
        "provider": backend.provider,
        "model": backend.model,
        "prompt_version": PROPOSE_VERSION,
        "sample_hash": identity,
        "sections": len(rows),
        "sections_with_proposals": covered,
        "coverage": round(covered / len(rows), 4) if rows else 0.0,
        "labels": proposals_frame.height,
        "distinct_labels": proposals_frame["label_norm"].n_unique(),
        **report,
    }
    _write_manifest(destination / PROPOSALS_MANIFEST, gold_dir, config, **summary)
    return {"output": str(destination / PROPOSALS), **summary}


_PROPOSALS_SCHEMA = {
    "section_key": pl.Utf8,
    "repo_id": pl.Int64,
    "full_name": pl.Utf8,
    "path": pl.Utf8,
    "tool": pl.Utf8,
    "kind": pl.Utf8,
    "heading_path": pl.List(pl.Utf8),
    "is_instruction": pl.Boolean,
    "rank": pl.Int64,
    "label": pl.Utf8,
    "label_norm": pl.Utf8,
    "definition": pl.Utf8,
    "request_key": pl.Utf8,
}


# --------------------------------------------------------------------------- consolidate


def label_groups(proposals: pl.DataFrame, max_definitions: int = 3) -> list[LabelGroup]:
    """Normalised labels, most proposed first, with their most common definitions."""
    definitions: dict[str, Counter[str]] = defaultdict(Counter)
    for label, definition in proposals.select("label_norm", "definition").iter_rows():
        definitions[label][definition] += 1
    grouped = (
        proposals.group_by("label_norm")
        .agg(
            pl.col("section_key").n_unique().alias("count"),
            pl.col("is_instruction").mean().alias("instruction_share"),
        )
        .sort(["count", "label_norm"], descending=[True, False])
    )
    return [
        LabelGroup(
            label,
            count,
            round(share, 2),
            tuple(
                text
                for text, _ in sorted(definitions[label].items(), key=lambda i: (-i[1], i[0]))[
                    :max_definitions
                ]
            ),
        )
        for label, count, share in grouped.iter_rows()
    ]


def consolidate_labels(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    *,
    seed_codebook: Path,
    allow_partial: bool = False,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Draft codebook (``codebook.draft.yaml``) and label -> category map from the proposals."""
    with run_lock(output_dir):
        return _consolidate_labels(
            gold_dir,
            output_dir,
            config,
            backend,
            seed_codebook,
            allow_partial,
            progress or Progress(),
        )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _single(
    backend: LLMBackend,
    prompt: PromptRequest,
    output_dir: Path,
    config: Rq1Config,
    progress: Progress,
    validate: Callable[[LLMResult], T],
) -> tuple[T, LLMResult]:
    """One request answered directly (not in a batch, so Claude refusal fallbacks apply), cached
    and validated; raises :class:`InvalidAnswer` when the answer is not usable."""
    cache = ResponseCache(output_dir / "cache")
    failures = FailureLog(output_dir / "failures.json")
    key = prompt.custom_id
    if failures.given_up([key], config.max_attempts):
        raise InvalidAnswer(
            f"{prompt.schema_name} failed {config.max_attempts} times; see "
            f"{output_dir / 'failures.json'}"
        )
    backend.observe_waits(progress.waiting)
    result = cache.get(key)
    if result is None:
        result = backend.complete(prompt)
        cache.put(result)
    answer = _settle(key, result, validate, cache, failures)
    if answer is None:
        raise InvalidAnswer(
            f"The {prompt.schema_name} answer was not usable ({failures.errors([key]).get(key)}). "
            "Run the command again to retry."
        )
    return answer, result


def _consolidate_labels(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    seed_codebook: Path,
    allow_partial: bool,
    progress: Progress,
) -> dict[str, Any]:
    _, identity = _load_sample(output_dir)
    source = phase_dir(output_dir, "propose", config.provider, config.propose_model)
    if not (source / PROPOSALS).exists():
        raise FileNotFoundError(f"No proposals in {source}: run `agm rq1 propose` first")
    proposed = _read_json(source / PROPOSALS_MANIFEST)
    if proposed["sample_hash"] != identity:
        raise SampleMismatchError(
            "The proposals were made for another sample: run `agm rq1 propose` again."
        )
    if proposed["coverage"] < config.min_proposal_coverage and not allow_partial:
        raise IncompleteProposalsError(
            f"Only {proposed['coverage']:.0%} of the sample has proposals (minimum "
            f"{config.min_proposal_coverage:.0%}). Run `agm rq1 propose` again to retry the "
            "missing ones, or pass --partial."
        )
    groups = label_groups(pl.read_parquet(source / PROPOSALS))
    reference = load_codebook(seed_codebook)
    minimum, maximum = config.target_categories
    provider, model = backend.provider, backend.model
    progress.set_total("steps", 3)

    # 1. Induce the categories from the labels alone (the reference is not shown).
    progress.set_phase(f"inducing categories from {len(groups)} labels with {model}")
    labels_text = render_labels(groups)
    induce_key = request_key(
        "induce", INDUCE_VERSION, provider, model, _sha(labels_text), f"{minimum}-{maximum}"
    )
    induced, induce_result = _single(
        backend,
        PromptRequest(
            induce_key,
            induce_system(minimum, maximum),
            labels_text,
            INDUCE_SCHEMA,
            config.consolidate_max_tokens,
            "codebook",
        ),
        output_dir,
        config,
        progress,
        lambda result: parse_induction(
            result.text,
            name="rq1",
            # Stable across days, so the mapping answers cached under it stay valid.
            version=f"draft-{induce_key[:8]}",
            source=(
                f"agm rq1 consolidate ({provider} {model}; {INDUCE_VERSION}, {ALIGN_VERSION}, "
                f"{MAP_VERSION}) from {len(groups)} labels proposed by {config.propose_model} "
                f"({proposed['prompt_version']}) for {proposed['sections_with_proposals']} "
                f"sections; reference: {reference.source}"
            ),
        ),
    )
    progress.advance("steps")

    # 2. Align each induced category with the reference taxonomy, separately.
    progress.set_phase(f"aligning {len(induced.categories)} categories with {reference.label}")
    align_text = render_alignment(induced, reference)
    alignment, align_result = _single(
        backend,
        PromptRequest(
            request_key("align", ALIGN_VERSION, provider, model, _sha(align_text)),
            ALIGN_SYSTEM,
            align_text,
            ALIGN_SCHEMA,
            config.map_max_tokens,
            "alignment",
        ),
        output_dir,
        config,
        progress,
        lambda result: parse_alignment(result.text, induced, reference),
    )
    codebook = with_alignment(induced, alignment, reference)
    progress.advance("steps")

    # 3. Map every proposed label to a category, in chunks.
    progress.set_phase(f"mapping {len(groups)} labels to {len(codebook.categories)} categories")
    prompts: list[PromptRequest] = []
    labels_of: dict[str, list[str]] = {}
    size = config.map_chunk_labels
    for start in range(0, len(groups), size):
        chunk = groups[start : start + size]
        labels = [group.label for group in chunk]
        key = request_key("map", MAP_VERSION, provider, model, codebook.fingerprint(), *labels)
        labels_of[key] = labels
        prompts.append(
            PromptRequest(
                key,
                MAP_SYSTEM,
                render_mapping(codebook, chunk),
                MAP_SCHEMA,
                config.map_max_tokens,
                "mapping",
            )
        )
    answers, report = _run_requests(
        backend,
        prompts,
        output_dir,
        config,
        progress,
        lambda prompt, result: parse_mapping(result.text, labels_of[prompt.custom_id], codebook),
    )
    mapping = {label: category for answer in answers.values() for label, category in answer.items()}
    progress.advance("steps")

    destination = phase_dir(output_dir, "consolidate", provider, model)
    destination.mkdir(parents=True, exist_ok=True)
    dump_codebook(codebook, destination / DRAFT_CODEBOOK)
    counts = {group.label: group for group in groups}
    label_map = pl.DataFrame(
        [
            {
                "label": label,
                "category": category,
                "sections": counts[label].count,
                "instruction_share": counts[label].instruction_share,
            }
            for label, category in sorted(mapping.items())
        ],
        schema={
            "label": pl.Utf8,
            "category": pl.Utf8,
            "sections": pl.Int64,
            "instruction_share": pl.Float64,
        },
    )
    _write_parquet(label_map, destination / LABEL_MAP)
    weight = label_map["sections"].sum() or 1
    usage = usage_totals([induce_result, align_result])
    for name, value in report["usage"].items():
        usage[name] = usage.get(name, 0) + value
    summary = {
        "provider": provider,
        "model": model,
        "prompt_versions": [INDUCE_VERSION, ALIGN_VERSION, MAP_VERSION],
        "sample_hash": identity,
        "proposals": str(source / PROPOSALS),
        "reference_codebook": {"path": str(seed_codebook), "fingerprint": reference.fingerprint()},
        "labels": len(groups),
        "categories": len(codebook.categories),
        "new_categories": sum(1 for c in codebook.categories if c.literature is None),
        "reference_categories_matched": len({v for v in alignment.values() if v}),
        "mapped_labels": len(mapping),
        "unmapped_labels": len(groups) - len(mapping),
        "other_share": round(
            label_map.filter(pl.col("category") == OTHER)["sections"].sum() / weight, 4
        ),
        "mapping_requests": {k: report[k] for k in ("requests", "answered", "failed", "given_up")},
        "usage": usage,
        "served_by": [r for r in (induce_result.served_by, align_result.served_by) if r],
    }
    _write_manifest(destination / CONSOLIDATE_MANIFEST, gold_dir, config, **summary)
    return {"output": str(destination / DRAFT_CODEBOOK), **summary}
