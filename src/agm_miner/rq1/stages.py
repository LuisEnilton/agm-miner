"""RQ1 stages. Each reads an analysis output (gold) and writes into
``DATA_DIR/rq1/<gold name>/`` with a JSON manifest next to every result.

Layout of that directory::

    sample.parquet, sample.json                  label-creation sample
    propose/<provider>/<model>/proposals.*        free labels per sampled section
    consolidate/<provider>/<model>/               codebook.draft.yaml, label_map.parquet
    classify/<codebook>/<provider>/<model>/       section, artifact and repository labels
    saturation/<codebook>/<provider>/<model>/     classification of a fresh sample
    validation/<codebook>/                        blind sheet, LLM replicates, agreement scores
    cache/, batches.json, failures.json           LLM answers, pending batches, failed requests
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

import polars as pl

from agm_miner.analysis.datasets import ANALYSIS_MANIFEST
from agm_miner.config import Rq1Config
from agm_miner.progress import Progress
from agm_miner.rq1.agreement import agreement
from agm_miner.rq1.backends import LLMBackend, PromptRequest
from agm_miner.rq1.codebook import OTHER, Codebook, dump_codebook, load_codebook
from agm_miner.rq1.execution import usage_totals
from agm_miner.rq1.llm import LLMResult
from agm_miner.rq1.prompts import (
    ALIGN_SCHEMA,
    ALIGN_SYSTEM,
    ALIGN_VERSION,
    CLASSIFY_SCHEMA,
    CLASSIFY_VERSION,
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
    classify_system,
    induce_system,
    normalise_label,
    parse_alignment,
    parse_classification,
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


# --------------------------------------------------------------------------- classify


class ValidationSheetExistsError(RuntimeError):
    """A validation sheet already exists (it may hold hand labels)."""


class InvalidSheetError(RuntimeError):
    """A filled validation sheet cannot be scored (wrong codebook or nothing labelled)."""


def distinct_units(units: pl.DataFrame) -> pl.DataFrame:
    """One unit per ``section_key`` (identical sections are classified once), represented by
    the occurrence with the smallest (repository id, path, position), as in the sampling."""
    return units.sort("section_key", "repo_id", "path", "position").unique(
        "section_key", keep="first", maintain_order=True
    )


def file_chunks(units: pl.DataFrame, size: int) -> list[list[dict[str, Any]]]:
    """Sections grouped by file (repository, path) in document order, in chunks of ``size``.
    Ordered by stable fields only, so the requests do not depend on database ids."""
    chunks: list[list[dict[str, Any]]] = []
    ordered = units.sort("repo_id", "path", "position")
    for _, group in ordered.group_by(["repo_id", "path"], maintain_order=True):
        rows = group.to_dicts()
        chunks.extend(rows[start : start + size] for start in range(0, len(rows), size))
    return chunks


def key_chunks(units: pl.DataFrame, size: int) -> list[list[dict[str, Any]]]:
    """Sections of a sample in ``section_key`` order, in chunks of ``size``."""
    rows = units.sort("section_key").to_dicts()
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def _classify(
    chunks: Sequence[Sequence[Mapping[str, Any]]],
    codebook: Codebook,
    backend: LLMBackend,
    output_dir: Path,
    config: Rq1Config,
    progress: Progress,
    *,
    tag: str = "",
) -> tuple[dict[str, tuple[str, ...]], dict[str, Any]]:
    """Categories of every section of ``chunks``; ``tag`` separates independent repetitions of
    the same requests (e.g. the stability replicates of the validation)."""
    system = classify_system(codebook)
    prompts: list[PromptRequest] = []
    ids_of: dict[str, dict[str, str]] = {}
    for chunk in chunks:
        keys = [row["section_key"] for row in chunk]
        key = request_key(
            "classify",
            CLASSIFY_VERSION,
            backend.provider,
            backend.model,
            codebook.fingerprint(),
            tag,
            *keys,
        )
        ids_of[key] = short_ids(keys)
        prompts.append(
            PromptRequest(
                key,
                system,
                render_sections(chunk, ids_of[key]),
                CLASSIFY_SCHEMA,
                config.classify_max_tokens,
                "classification",
            )
        )
    answers, report = _run_requests(
        backend,
        prompts,
        output_dir,
        config,
        progress,
        lambda prompt, result: parse_classification(
            result.text, ids_of[prompt.custom_id], codebook
        ),
    )
    labels = {key: cats for answer in answers.values() for key, cats in answer.items()}
    return labels, report


def _codebook_slug(codebook: Codebook) -> str:
    return model_slug(codebook.label)


def classify_sections(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    *,
    codebook_path: Path,
    limit: int | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Categories of every section with text, sent file by file (``limit`` requests at most)."""
    with run_lock(output_dir):
        return _classify_sections(
            gold_dir, output_dir, config, backend, codebook_path, limit, progress or Progress()
        )


def _classify_sections(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    codebook_path: Path,
    limit: int | None,
    progress: Progress,
) -> dict[str, Any]:
    codebook = load_codebook(codebook_path)
    units = load_units(gold_dir)
    distinct = distinct_units(units)
    chunks = file_chunks(distinct, config.chunk_sections)
    selected = chunks if limit is None else chunks[:limit]
    progress.set_phase(f"classifying {sum(len(c) for c in selected)} sections with {backend.model}")
    labels, report = _classify(selected, codebook, backend, output_dir, config, progress)

    section_labels = _section_labels(units, labels)
    artifact_labels = _artifact_labels(section_labels)
    destination = output_dir / "classify" / _codebook_slug(codebook) / backend.provider
    destination = destination / model_slug(backend.model)
    outputs = {
        "section_labels": section_labels,
        "artifact_labels": artifact_labels,
        "repo_labels": _repo_labels(section_labels),
        "label_prevalence": _label_prevalence(artifact_labels, section_labels),
    }
    for name, frame in outputs.items():
        _write_parquet(frame, destination / f"{name}.parquet")
    labelled = len(labels)
    summary = {
        "provider": backend.provider,
        "model": backend.model,
        "prompt_version": CLASSIFY_VERSION,
        "codebook": {
            "path": str(codebook_path),
            "label": codebook.label,
            "fingerprint": codebook.fingerprint(),
        },
        "sections": units.height,
        "distinct_sections": distinct.height,
        "requests_planned": len(chunks),
        "limit": limit,
        "distinct_sections_labelled": labelled,
        "coverage": round(labelled / distinct.height, 4) if distinct.height else 0.0,
        "other_share": round(sum(1 for cats in labels.values() if cats == (OTHER,)) / labelled, 4)
        if labelled
        else None,
        "rows": {name: frame.height for name, frame in outputs.items()},
        **report,
    }
    _write_manifest(destination / "classify.json", gold_dir, config, **summary)
    return {"output": str(destination), **summary}


_SECTION_LABELS_SCHEMA = {
    "section_key": pl.Utf8,
    "repo_id": pl.Int64,
    "full_name": pl.Utf8,
    "artifact_id": pl.Int64,
    "path": pl.Utf8,
    "tool": pl.Utf8,
    "kind": pl.Utf8,
    "position": pl.Int64,
    "words": pl.Int64,
    "category": pl.Utf8,
}


def _section_labels(units: pl.DataFrame, labels: Mapping[str, tuple[str, ...]]) -> pl.DataFrame:
    """One row per (section occurrence, category): identical sections share their labels."""
    rows = []
    for unit in units.iter_rows(named=True):
        for category in labels.get(unit["section_key"], ()):
            rows.append(
                {name: unit[name] for name in _SECTION_LABELS_SCHEMA if name != "category"}
                | {"category": category}
            )
    return pl.DataFrame(rows, schema=_SECTION_LABELS_SCHEMA)


def _artifact_labels(section_labels: pl.DataFrame) -> pl.DataFrame:
    """Per file and category: sections, words and share of the file's classified words."""
    words = (
        section_labels.unique(["artifact_id", "section_key", "position"])
        .group_by("artifact_id")
        .agg(pl.col("words").sum().alias("artifact_words"))
    )
    return (
        section_labels.group_by(
            "repo_id", "full_name", "artifact_id", "path", "tool", "kind", "category"
        )
        .agg(pl.len().alias("sections"), pl.col("words").sum().alias("words"))
        .join(words, on="artifact_id")
        .with_columns(
            (pl.col("words") / pl.col("artifact_words")).fill_nan(0.0).alias("word_share")
        )
        .sort("repo_id", "path", "category")
    )


def _repo_labels(section_labels: pl.DataFrame) -> pl.DataFrame:
    """Per repository and category (joins repo_features on repo_id for the correlations)."""
    words = (
        section_labels.unique(["artifact_id", "section_key", "position"])
        .group_by("repo_id")
        .agg(pl.col("words").sum().alias("repo_words"))
    )
    return (
        section_labels.group_by("repo_id", "category")
        .agg(
            pl.col("artifact_id").n_unique().alias("artifacts"),
            pl.len().alias("sections"),
            pl.col("words").sum().alias("words"),
        )
        .join(words, on="repo_id")
        .with_columns((pl.col("words") / pl.col("repo_words")).fill_nan(0.0).alias("word_share"))
        .sort("repo_id", "category")
    )


def _label_prevalence(artifact_labels: pl.DataFrame, section_labels: pl.DataFrame) -> pl.DataFrame:
    """Per tool, artifact kind and category: share of files and of sections with the category."""
    files = artifact_labels.group_by("tool", "kind").agg(
        pl.col("artifact_id").n_unique().alias("files_total")
    )
    sections = (
        section_labels.unique(["artifact_id", "section_key", "position"])
        .group_by("tool", "kind")
        .agg(pl.len().alias("sections_total"))
    )
    return (
        artifact_labels.group_by("tool", "kind", "category")
        .agg(pl.col("artifact_id").n_unique().alias("files"), pl.col("sections").sum())
        .join(files, on=["tool", "kind"])
        .join(sections, on=["tool", "kind"])
        .with_columns(
            (pl.col("files") / pl.col("files_total")).alias("file_share"),
            (pl.col("sections") / pl.col("sections_total")).alias("section_share"),
        )
        .sort("tool", "kind", "file_share", descending=[False, False, True])
    )


# --------------------------------------------------------------------------- saturation


def saturation(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    *,
    codebook_path: Path,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Classify a fresh sample (none of the label-creation sections): the share that fits no
    category tells whether the codebook covers the data."""
    with run_lock(output_dir):
        progress = progress or Progress()
        codebook = load_codebook(codebook_path)
        label_sample, _ = _load_sample(output_dir)
        fresh = stratified_sample(
            load_units(gold_dir),
            config.saturation_size,
            per_repo_cap=config.per_repo_cap,
            seed=config.seed + 1,
            length_bins=config.length_bins,
            exclude_keys=label_sample["section_key"],
        )
        progress.set_phase(f"classifying {fresh.height} fresh sections with {backend.model}")
        labels, report = _classify(
            key_chunks(fresh, config.propose_chunk_sections),
            codebook,
            backend,
            output_dir,
            config,
            progress,
        )
        counts = Counter(category for cats in labels.values() for category in cats)
        destination = output_dir / "saturation" / _codebook_slug(codebook) / backend.provider
        destination = destination / model_slug(backend.model)
        _write_parquet(
            pl.DataFrame(
                [{"section_key": k, "categories": list(v)} for k, v in sorted(labels.items())],
                schema={"section_key": pl.Utf8, "categories": pl.List(pl.Utf8)},
            ),
            destination / "saturation.parquet",
        )
        labelled = len(labels)
        summary = {
            "provider": backend.provider,
            "model": backend.model,
            "codebook": codebook.label,
            "sample": fresh.height,
            "labelled": labelled,
            "other_share": round(counts[OTHER] / labelled, 4) if labelled else None,
            "categories_used": len([c for c in counts if c != OTHER]),
            "categories_unused": sorted(set(codebook.ids) - set(counts)),
            "category_counts": dict(counts.most_common()),
            **report,
        }
        _write_manifest(destination / "saturation.json", gold_dir, config, **summary)
        return {"output": str(destination), **summary}


# --------------------------------------------------------------------------- validation

_TRUE = {"1", "x", "yes", "y", "sim", "s", "true"}


def _validation_dir(output_dir: Path, codebook: Codebook) -> Path:
    return output_dir / "validation" / _codebook_slug(codebook)


def export_validation(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    *,
    codebook_path: Path,
    size: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Blind sheet (CSV) to label by hand: a fresh stratified sample, one 0/1 column per
    category, without the LLM's answers."""
    with run_lock(output_dir):
        codebook = load_codebook(codebook_path)
        destination = _validation_dir(output_dir, codebook)
        sheet = destination / "sheet.csv"
        if sheet.exists() and not force:
            raise ValidationSheetExistsError(
                f"{sheet} already exists and may hold your labels; use --force to replace it."
            )
        label_sample, _ = _load_sample(output_dir)
        sample = stratified_sample(
            load_units(gold_dir),
            size or config.validation_size,
            per_repo_cap=config.per_repo_cap,
            seed=config.seed + 2,
            length_bins=config.length_bins,
            exclude_keys=label_sample["section_key"],
        ).sort("section_key")
        _write_parquet(sample, destination / "sample.parquet")
        columns = [*codebook.ids, OTHER]
        sheet_frame = sample.select(
            "section_key",
            pl.col("full_name").alias("repository"),
            pl.col("path").alias("file"),
            pl.col("heading_path").list.join(" > ").alias("heading_path"),
            "text",
        ).with_columns(*[pl.lit("").alias(column) for column in columns], pl.lit("").alias("notes"))
        sheet_frame.write_csv(sheet)
        summary = {
            "sheet": str(sheet),
            "sections": sample.height,
            "codebook": codebook.label,
            "columns": columns,
            "instructions": "Mark 1 (or x) in every category column that applies; mark 'other' "
            "only when none does. Leave a row blank to skip it.",
        }
        _write_manifest(destination / "export.json", gold_dir, config, **summary)
        return summary


def validation_llm(
    gold_dir: Path,
    output_dir: Path,
    config: Rq1Config,
    backend: LLMBackend,
    *,
    codebook_path: Path,
    replicate: int = 1,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """The LLM's labels for the validation sample; replicates are independent repetitions of
    the same requests (the APIs offer no fixed sampling), used to measure stability."""
    with run_lock(output_dir):
        progress = progress or Progress()
        codebook = load_codebook(codebook_path)
        destination = _validation_dir(output_dir, codebook)
        sample_path = destination / "sample.parquet"
        if not sample_path.exists():
            raise FileNotFoundError("No validation sample: run `agm rq1 validate export` first")
        sample = pl.read_parquet(sample_path)
        labels, report = _classify(
            key_chunks(sample, config.propose_chunk_sections),
            codebook,
            backend,
            output_dir,
            config,
            progress,
            tag=f"validation-replicate-{replicate}",
        )
        target = destination / "llm" / backend.provider / model_slug(backend.model)
        _write_parquet(
            pl.DataFrame(
                [{"section_key": k, "categories": list(v)} for k, v in sorted(labels.items())],
                schema={"section_key": pl.Utf8, "categories": pl.List(pl.Utf8)},
            ),
            target / f"replicate-{replicate}.parquet",
        )
        summary = {
            "provider": backend.provider,
            "model": backend.model,
            "replicate": replicate,
            "sections": sample.height,
            "labelled": len(labels),
            **report,
        }
        _write_manifest(target / f"replicate-{replicate}.json", gold_dir, config, **summary)
        return {"output": str(target / f"replicate-{replicate}.parquet"), **summary}


def read_sheet(path: Path, categories: Sequence[str]) -> dict[str, frozenset[str]]:
    """Hand labels of a filled sheet: section key -> categories (rows left blank are skipped)."""
    sheet = pl.read_csv(path, infer_schema=False)
    missing = sorted(set(categories) - set(sheet.columns))
    if missing:
        raise InvalidSheetError(
            f"{path} has no column for {missing}: was it exported with this codebook?"
        )
    labels: dict[str, frozenset[str]] = {}
    for row in sheet.iter_rows(named=True):
        chosen = frozenset(c for c in categories if str(row.get(c) or "").strip().lower() in _TRUE)
        if chosen:
            labels[row["section_key"]] = chosen - {OTHER} or frozenset({OTHER})
    return labels


def _replicate_labels(path: Path) -> dict[str, frozenset[str]]:
    frame = pl.read_parquet(path)
    return {key: frozenset(cats) for key, cats in frame.iter_rows()}


def score_validation(
    output_dir: Path,
    config: Rq1Config,
    *,
    codebook_path: Path,
    human: Path | None = None,
    retest: Path | None = None,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Every agreement the available labels allow: the LLM with the human labels, the LLM
    with itself (stability, from replicates 1 and 2) and the human with a later re-labelling of
    part of the sheet (intra-rater). Without hand labels, only the stability is measured."""
    codebook = load_codebook(codebook_path)
    categories = [*codebook.ids, OTHER]
    destination = _validation_dir(output_dir, codebook)
    llm_dir = destination / "llm" / provider / model_slug(model)
    first = llm_dir / "replicate-1.parquet"
    if not first.exists():
        raise FileNotFoundError("No LLM labels: run `agm rq1 validate llm` first")
    human_labels = read_sheet(human, categories) if human is not None else {}
    if human is not None and not human_labels:
        raise InvalidSheetError(f"{human} has no labelled row yet")
    scores: dict[str, Any] = {
        "codebook": codebook.label,
        "provider": provider,
        "model": model,
        "human_sections": len(human_labels),
    }
    if human_labels:
        scores["human_vs_llm"] = agreement(human_labels, _replicate_labels(first), categories)
    second = llm_dir / "replicate-2.parquet"
    if second.exists():
        scores["llm_stability"] = agreement(
            _replicate_labels(first), _replicate_labels(second), categories
        )
    if retest is not None and human_labels:
        scores["human_retest"] = agreement(human_labels, read_sheet(retest, categories), categories)
    if len(scores) == 4:  # nothing beyond the header fields
        raise InvalidSheetError(
            "Nothing to compare: label the sheet (--human) or run `agm rq1 validate llm "
            "--replicate 2` for the stability."
        )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "scores.json").write_text(json.dumps(scores, indent=2), encoding="utf-8")
    (destination / "scores.md").write_text(_scores_markdown(scores), encoding="utf-8")
    return {"output": str(destination / "scores.json"), **_score_summary(scores)}


def _score_summary(scores: Mapping[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"human_sections": scores["human_sections"]}
    for comparison in ("human_vs_llm", "llm_stability", "human_retest"):
        if comparison in scores:
            result = scores[comparison]
            summary[comparison] = {
                key: result[key]
                for key in (
                    "sections",
                    "exact_match",
                    "macro_kappa",
                    "macro_kappa_ci95",
                    "macro_f1",
                )
            }
    return summary


def _scores_markdown(scores: Mapping[str, Any]) -> str:
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value:.2f}"

    lines = [f"# RQ1 agreement ({scores['codebook']}, {scores['provider']} {scores['model']})", ""]
    titles = {
        "human_vs_llm": "Human × LLM",
        "llm_stability": "LLM × LLM (stability)",
        "human_retest": "Human × human (re-test)",
    }
    for comparison, title in titles.items():
        if comparison not in scores:
            continue
        result = scores[comparison]
        lines += [
            f"## {title}",
            "",
            f"{result['sections']} sections; exact match {fmt(result['exact_match'])}; "
            f"macro kappa {fmt(result['macro_kappa'])} {result['macro_kappa_ci95'] or ''}; "
            f"macro F1 {fmt(result['macro_f1'])}",
            "",
            "| category | kappa | precision | recall | F1 | prevalence A | prevalence B |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in result["categories"]:
            lines.append(
                f"| {row['category']} | {fmt(row['kappa'])} | {fmt(row['precision'])} | "
                f"{fmt(row['recall'])} | {fmt(row['f1'])} | {row['prevalence_a']:.2f} | "
                f"{row['prevalence_b']:.2f} |"
            )
        lines.append("")
    return "\n".join(lines)
