"""The RQ1 codebook: categories with definitions and inclusion/exclusion criteria.

The curated codebook is a methodological artifact versioned with the code (``codebooks/``), like
``study.yaml``. Every classification records its ``fingerprint``, so labels can always be traced
back to the exact definitions the model saw.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Reserved answer for sections that fit no category (never a category id).
OTHER = "other"
_ID_PATTERN = r"^[a-z][a-z0-9_]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Category(_Strict):
    id: str = Field(pattern=_ID_PATTERN)
    name: str
    group: str = ""
    definition: str
    include: str = ""  # when to apply it
    exclude: str = ""  # near misses that belong elsewhere
    examples: list[str] = Field(default_factory=list)
    # Matching label of related work (e.g. "Agent READMEs: Testing"), or None if new.
    literature: str | None = None

    @field_validator("id")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value == OTHER:
            raise ValueError(f'"{OTHER}" is reserved for sections that fit no category')
        return value


class Codebook(_Strict):
    name: str
    version: str
    description: str = ""
    source: str = ""  # where the categories come from (paper, draft run, curation notes)
    categories: list[Category]

    @model_validator(mode="after")
    def _unique_ids(self) -> Codebook:
        ids = [category.id for category in self.categories]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            raise ValueError(f"duplicated category ids: {duplicated}")
        return self

    @property
    def ids(self) -> list[str]:
        return [category.id for category in self.categories]

    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        """Short identifier for paths and manifests, e.g. ``rq1@1.0``."""
        return f"{self.name}@{self.version}"


def load_codebook(path: Path) -> Codebook:
    return Codebook.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def dump_codebook(codebook: Codebook, path: Path) -> None:
    data = codebook.model_dump(mode="json")
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )
