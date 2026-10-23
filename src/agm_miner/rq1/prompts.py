"""Prompts, output schemas and answer validation of the RQ1 label-creation phases (pure).

Every prompt has a version that is part of the cache key: change the text, change the version,
and the next run asks again instead of reusing answers to a different question. Rendering is
deterministic (the same inputs always give the same bytes), which the cache depends on.

Label creation, in four steps:

* **propose**: free labels (1 to 3) for a chunk of sampled sections, plus whether each section is
  an instruction to the agent or only a description of the project. The prompt cites no example
  labels, so it does not steer the vocabulary.
* **induce**: the proposed labels (normalised, with counts and definitions) become categories.
  The model does not see the reference taxonomy here: shown together, it copied it (pilot of
  22/10/2026), which would make the codebook deductive.
* **align**: each induced category is matched, in a separate request, to the reference taxonomy
  of related work (or marked as new), for comparability.
* **map**: the proposed labels are assigned to the induced categories, in chunks.

Sections are shown to the model with short ids (``s1``, ``s2``...) mapped back to their
``section_key``, so the model never has to copy a 64-character hash.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml
from pydantic import ValidationError

from agm_miner.rq1.codebook import OTHER, Codebook

PROPOSE_VERSION = "propose-v2"
INDUCE_VERSION = "induce-v1"
ALIGN_VERSION = "align-v1"
MAP_VERSION = "map-v1"
MAX_LABELS_PER_SECTION = 3


class InvalidAnswer(ValueError):
    """The model answered, but not what the prompt asked for (the answer is not kept)."""


# --------------------------------------------------------------------------- propose

PROPOSE_SYSTEM = f"""\
You are helping researchers build a taxonomy of the guidance developers write for AI coding \
agents in files such as CLAUDE.md, AGENTS.md and Claude Code skills (SKILL.md).

You will receive several sections taken from such files. For each section:

1. Propose between 1 and {MAX_LABELS_PER_SECTION} labels describing what kind of guidance the \
section gives the agent: its topic and its purpose, not the specific project. A label is a \
short noun phrase (two to five words) that would fit sections of many other projects. Never use \
a project or product name, or only the name of a technology or tool, as a label, and avoid \
vague labels that would fit any section.
2. Give each label a one-sentence definition precise enough for another person to apply it \
consistently.
3. Say whether the section is an instruction (it tells the agent what to do or avoid, how to \
behave, or which commands to run) or only describes the project.

Answer only with the JSON object requested: one entry per section, with the section ids exactly \
as given."""

PROPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "is_instruction": {"type": "boolean"},
                    "labels": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "definition": {"type": "string"},
                            },
                            "required": ["name", "definition"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "is_instruction", "labels"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sections"],
    "additionalProperties": False,
}


def short_ids(section_keys: Sequence[str]) -> dict[str, str]:
    """``s1``, ``s2``... for the sections of one request, in order."""
    return {f"s{index}": key for index, key in enumerate(section_keys, start=1)}


def render_sections(sections: Sequence[Mapping[str, Any]], ids: Mapping[str, str]) -> str:
    """The sections of one request, each tagged with its short id and its context."""
    by_key = {section["section_key"]: section for section in sections}
    blocks = []
    for short_id, key in ids.items():
        section = by_key[key]
        heading = " > ".join(section.get("heading_path") or []) or "(no heading)"
        blocks.append(
            f'<section id="{short_id}">\n'
            f"repository: {section['full_name']}\n"
            f"file: {section['path']} ({section['tool']}, {section['kind']})\n"
            f"heading path: {heading}\n"
            f"text:\n{section['text'].strip()}\n"
            "</section>"
        )
    return "\n\n".join(blocks)


@dataclass(frozen=True, slots=True)
class Proposal:
    section_key: str
    is_instruction: bool
    labels: tuple[tuple[str, str], ...]  # (name, definition), in the model's order


def _decode(text: str | None) -> Any:
    if not text:
        raise InvalidAnswer("empty answer")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidAnswer(f"not JSON: {exc}") from exc


def parse_proposals(text: str | None, ids: Mapping[str, str]) -> list[Proposal]:
    """Proposals of every section of the request; :class:`InvalidAnswer` unless each section
    appears once with at least one named, defined label (extra labels beyond the maximum are
    dropped)."""
    data = _decode(text)
    entries = data.get("sections") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise InvalidAnswer('no "sections" list')
    proposals: dict[str, Proposal] = {}
    for entry in entries:
        short_id = entry.get("id") if isinstance(entry, dict) else None
        if short_id not in ids:
            raise InvalidAnswer(f"unknown section id {short_id!r}")
        if short_id in proposals:
            raise InvalidAnswer(f"section {short_id} answered twice")
        labels = tuple(
            (str(label.get("name", "")).strip(), str(label.get("definition", "")).strip())
            for label in entry.get("labels") or []
            if isinstance(label, dict)
        )
        labels = tuple(label for label in labels if label[0] and label[1])
        if not labels:
            raise InvalidAnswer(f"section {short_id} has no named, defined label")
        proposals[short_id] = Proposal(
            ids[short_id], bool(entry.get("is_instruction")), labels[:MAX_LABELS_PER_SECTION]
        )
    missing = sorted(set(ids) - set(proposals), key=lambda s: int(s[1:]))
    if missing:
        raise InvalidAnswer(f"sections without an answer: {', '.join(missing)}")
    return [proposals[short_id] for short_id in ids]


# --------------------------------------------------------------------------- consolidate

_NORMALISE_RE = re.compile(r"[\s_\-/]+")


def normalise_label(name: str) -> str:
    """Case, separators and trailing punctuation do not make labels different."""
    return _NORMALISE_RE.sub(" ", name.strip().lower()).strip(" .:;,")


@dataclass(frozen=True, slots=True)
class LabelGroup:
    """One normalised label with how often it was proposed and how it was defined."""

    label: str
    count: int
    instruction_share: float
    definitions: tuple[str, ...] = field(default=())


_CATEGORY_PROPERTIES: dict[str, Any] = {
    "id": {"type": "string"},
    "name": {"type": "string"},
    "group": {"type": "string"},
    "definition": {"type": "string"},
    "include": {"type": "string"},
    "exclude": {"type": "string"},
    "examples": {"type": "array", "items": {"type": "string"}},
}


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _list_of(key: str, item: dict[str, Any]) -> dict[str, Any]:
    return _object({key: {"type": "array", "items": item}})


def render_labels(groups: Sequence[LabelGroup]) -> str:
    lines = [
        f"Proposed labels ({len(groups)} distinct; n = sections, instruction share = fraction of "
        "those sections that instruct the agent):"
    ]
    for group in groups:
        definitions = " | ".join(group.definitions)
        lines.append(
            f"- {group.label} (n={group.count}, instruction share "
            f"{group.instruction_share:.2f}): {definitions}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------- induce


def induce_system(min_categories: int, max_categories: int) -> str:
    return f"""\
You build a codebook for qualitative content analysis from free-form labels that were proposed \
for sections of instruction files for AI coding agents (CLAUDE.md, AGENTS.md, SKILL.md).

Group the labels into between {min_categories} and {max_categories} categories that are clearly \
distinct from each other and together cover the labels. Derive the categories from the labels \
themselves. For each category give:
- id: snake_case, unique, never "{OTHER}";
- name and group (a broader theme shared by related categories);
- definition: what the category covers;
- include: when to apply it; exclude: near misses that belong to another category (name it);
- examples: 2 or 3 of the input labels it covers.

Answer only with the JSON object requested."""


INDUCE_SCHEMA = _list_of("categories", _object(_CATEGORY_PROPERTIES))


def parse_induction(text: str | None, *, name: str, version: str, source: str) -> Codebook:
    data = _decode(text)
    if not isinstance(data, dict) or not isinstance(data.get("categories"), list):
        raise InvalidAnswer('no "categories" list')
    if not data["categories"]:
        raise InvalidAnswer("no categories")
    try:
        return Codebook(name=name, version=version, source=source, categories=data["categories"])
    except ValidationError as exc:
        raise InvalidAnswer(f"invalid codebook: {exc.errors()[0]['msg']}") from exc


# ----------------------------------------------------------------- align

ALIGN_SYSTEM = """\
You compare two taxonomies of the guidance written for AI coding agents. The first was induced \
from data in this study; the second comes from related work.

For each category of the induced taxonomy, give the id of the reference category that covers \
the same guidance, or an empty string when none does (a new category). Several induced \
categories may match the same reference category. Judge by the definitions, not by the names. \
Answer only with the JSON object requested, with one entry per induced category."""

ALIGN_SCHEMA = _list_of(
    "alignment", _object({"category": {"type": "string"}, "reference": {"type": "string"}})
)


def _taxonomy(codebook: Codebook) -> str:
    entries = [
        {"id": c.id, "name": c.name, "definition": c.definition} for c in codebook.categories
    ]
    return yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=100).strip()


def render_alignment(induced: Codebook, reference: Codebook) -> str:
    return (
        f"Induced taxonomy:\n{_taxonomy(induced)}\n\n"
        f"Reference taxonomy ({reference.source or reference.label}):\n{_taxonomy(reference)}"
    )


def parse_alignment(text: str | None, induced: Codebook, reference: Codebook) -> dict[str, str]:
    """Induced category id -> reference category id ("" for new ones)."""
    data = _decode(text)
    entries = data.get("alignment") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise InvalidAnswer('no "alignment" list')
    known, targets = set(induced.ids), set(reference.ids)
    alignment: dict[str, str] = {}
    for entry in entries:
        category = entry.get("category") if isinstance(entry, dict) else None
        target = str(entry.get("reference") or "") if isinstance(entry, dict) else ""
        if category not in known:
            raise InvalidAnswer(f"unknown induced category {category!r}")
        if target and target not in targets:
            raise InvalidAnswer(f"unknown reference category {target!r}")
        alignment[category] = target
    missing = sorted(known - set(alignment))
    if missing:
        raise InvalidAnswer(f"categories without an alignment: {', '.join(missing)}")
    return alignment


def with_alignment(
    induced: Codebook, alignment: Mapping[str, str], reference: Codebook
) -> Codebook:
    """The induced codebook with ``literature`` naming the matching reference category."""
    title = reference.description or reference.name
    names = {category.id: category.name for category in reference.categories}
    categories = [
        category.model_copy(
            update={
                "literature": f"{title}: {names[alignment[category.id]]}"
                if alignment.get(category.id)
                else None
            }
        )
        for category in induced.categories
    ]
    return induced.model_copy(update={"categories": categories})


# ----------------------------------------------------------------- map

MAP_SYSTEM = f"""\
You assign free-form labels, proposed for sections of instruction files for AI coding agents, to \
the categories of a codebook. Assign every label to the one category whose definition and \
inclusion criteria fit it best, respecting the exclusion criteria, or to "{OTHER}" when no \
category fits. Answer only with the JSON object requested, with every label exactly as given."""

MAP_SCHEMA = _list_of(
    "mapping", _object({"label": {"type": "string"}, "category": {"type": "string"}})
)


def render_mapping(codebook: Codebook, labels: Sequence[LabelGroup]) -> str:
    categories = [
        {
            "id": c.id,
            "name": c.name,
            "definition": c.definition,
            "include": c.include,
            "exclude": c.exclude,
        }
        for c in codebook.categories
    ]
    codebook_text = yaml.safe_dump(categories, sort_keys=False, allow_unicode=True, width=100)
    return f"Codebook:\n{codebook_text.strip()}\n\n{render_labels(labels)}"


def parse_mapping(text: str | None, labels: Sequence[str], codebook: Codebook) -> dict[str, str]:
    """Label -> category id (or "other") for every label of the request."""
    data = _decode(text)
    entries = data.get("mapping") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise InvalidAnswer('no "mapping" list')
    wanted, targets = set(labels), set(codebook.ids) | {OTHER}
    mapping: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise InvalidAnswer("a mapping entry is not an object")
        label = normalise_label(str(entry.get("label", "")))
        category = str(entry.get("category", ""))
        if label not in wanted:
            raise InvalidAnswer(f"unknown label {label!r}")
        if category not in targets:
            raise InvalidAnswer(f"unknown category {category!r} for {label!r}")
        mapping[label] = category
    missing = sorted(wanted - set(mapping))
    if missing:
        raise InvalidAnswer(f"{len(missing)} labels without a category, e.g. {missing[0]!r}")
    return mapping
