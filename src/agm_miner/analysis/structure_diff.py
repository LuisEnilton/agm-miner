"""What changed between two versions of an artifact, at the level of sections and frontmatter
fields (RQ3: "which parts are added and removed, and after which triggers")."""

from __future__ import annotations

import hashlib
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


def _keyed_sections(document: ParsedDocument) -> dict[str, str]:
    """Sections by heading path; repeated paths get an occurrence suffix (``#2``)."""
    seen: Counter[str] = Counter()
    result: dict[str, str] = {}
    for section in document.sections:
        key = section_key(section)
        seen[key] += 1
        if seen[key] > 1:
            key = f"{key} #{seen[key]}"
        result[key] = hashlib.sha1(section.text.encode("utf-8")).hexdigest()
    return result


def diff_documents(before: ParsedDocument | None, after: ParsedDocument | None) -> StructureDiff:
    """Diff two parsed versions; ``None`` stands for "did not exist"."""
    old_sections = _keyed_sections(before) if before else {}
    new_sections = _keyed_sections(after) if after else {}
    old_front: dict[str, Any] = (before.frontmatter or {}) if before else {}
    new_front: dict[str, Any] = (after.frontmatter or {}) if after else {}
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
