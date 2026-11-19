"""What changed between two versions of an artifact, at the level of sections and frontmatter
fields (RQ3: "which parts are added and removed, and after which triggers")."""

from __future__ import annotations

import hashlib
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from agm_miner.analysis.markdown import ParsedDocument, SectionRecord


@dataclass(frozen=True, slots=True)
class StructureDiff:
    sections_added: list[str] = field(default_factory=list)
    sections_removed: list[str] = field(default_factory=list)
    sections_modified: list[str] = field(default_factory=list)
    frontmatter_added: list[str] = field(default_factory=list)
    frontmatter_removed: list[str] = field(default_factory=list)
    frontmatter_modified: list[str] = field(default_factory=list)

    @property
    def is_structural(self) -> bool:
        """True when sections or frontmatter keys appeared or disappeared."""
        return bool(
            self.sections_added
            or self.sections_removed
            or self.frontmatter_added
            or self.frontmatter_removed
        )


def section_key(section: SectionRecord) -> str:
    return " > ".join(section.path) if section.path else "(preamble)"


@dataclass(frozen=True, slots=True)
class DocumentOutline:
    """What ``diff_documents`` compares, without the text: a digest of every section by its
    key and the frontmatter. The analysis keeps one per blob for the whole RQ3 step, so it holds
    no section text (``ParsedDocument`` holds the whole document several times)."""

    sections: tuple[tuple[str, bytes], ...]
    frontmatter: dict[str, Any]


def _keyed_sections(document: ParsedDocument) -> tuple[tuple[str, bytes], ...]:
    """Sections by heading path; repeated paths get an occurrence suffix (``#2``). Keys are
    interned: the same headings repeat across thousands of files."""
    seen: Counter[str] = Counter()
    result: list[tuple[str, bytes]] = []
    for section in document.sections:
        key = section_key(section)
        seen[key] += 1
        if seen[key] > 1:
            key = f"{key} #{seen[key]}"
        digest = hashlib.blake2b(section.text.encode("utf-8"), digest_size=16).digest()
        result.append((sys.intern(key), digest))
    return tuple(result)


def outline(document: ParsedDocument) -> DocumentOutline:
    return DocumentOutline(_keyed_sections(document), document.frontmatter or {})


def _as_outline(document: ParsedDocument | DocumentOutline | None) -> DocumentOutline | None:
    if document is None or isinstance(document, DocumentOutline):
        return document
    return outline(document)


def diff_documents(
    before: ParsedDocument | DocumentOutline | None,
    after: ParsedDocument | DocumentOutline | None,
) -> StructureDiff:
    """Diff two parsed versions (or their outlines); ``None`` stands for "did not exist"."""
    old, new = _as_outline(before), _as_outline(after)
    old_sections = dict(old.sections) if old else {}
    new_sections = dict(new.sections) if new else {}
    old_front: dict[str, Any] = old.frontmatter if old else {}
    new_front: dict[str, Any] = new.frontmatter if new else {}
    return StructureDiff(
        sections_added=[key for key in new_sections if key not in old_sections],
        sections_removed=[key for key in old_sections if key not in new_sections],
        sections_modified=[
            key
            for key in new_sections
            if key in old_sections and new_sections[key] != old_sections[key]
        ],
        frontmatter_added=sorted(set(new_front) - set(old_front)),
        frontmatter_removed=sorted(set(old_front) - set(new_front)),
        frontmatter_modified=sorted(
            key for key in set(new_front) & set(old_front) if new_front[key] != old_front[key]
        ),
    )
