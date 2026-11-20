"""The publication metadata (CITATION.cff, .zenodo.json, CHANGELOG.md) agrees with pyproject."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_citation_matches_the_package() -> None:
    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text())
    project = _project()
    assert citation["cff-version"] == "1.2.0"
    assert citation["version"] == project["version"]
    assert citation["license"] == project["license"] == "MIT"
    assert citation["repository-code"] == "https://github.com/LuisEnilton/agm-miner"
    assert all(author["given-names"] and author["family-names"] for author in citation["authors"])


def test_zenodo_metadata_matches_the_citation() -> None:
    zenodo = json.loads((ROOT / ".zenodo.json").read_text())
    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text())
    assert zenodo["upload_type"] == "software" and zenodo["license"] == "MIT"
    assert zenodo["title"] == citation["title"]
    names = [f"{a['family-names']}, {a['given-names']}" for a in citation["authors"]]
    assert [creator["name"] for creator in zenodo["creators"]] == names


def test_changelog_starts_with_the_current_version() -> None:
    first = re.search(r"^## \[([^\]]+)\]", (ROOT / "CHANGELOG.md").read_text(), re.MULTILINE)
    assert first is not None and first.group(1) == _project()["version"]


def test_license_is_mit() -> None:
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
