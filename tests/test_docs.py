"""The documentation stays in step with the code: every command is documented and every
relative link (file and anchor) of README.md, AGENTS.md and docs/ resolves."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from agm_miner.cli import app
from agm_miner.cli.ops import _command_lines

ROOT = Path(__file__).resolve().parents[1]
DOCS = [ROOT / "README.md", ROOT / "AGENTS.md", *sorted((ROOT / "docs").glob("*.md"))]
LINK = re.compile(r"\]\(([^)\s]+)\)")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[`*_]", "", heading.strip().lower())
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    headings = re.findall(r"^#{1,6} (.+)$", text, re.MULTILINE)
    explicit = re.findall(r'<a id="([^"]+)"></a>', text)
    return {_slug(heading) for heading in headings} | set(explicit)


def test_every_command_is_in_the_cli_reference() -> None:
    reference = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    commands = [name for name, _, _ in _command_lines(typer.main.get_command(app), "")]
    missing = [name for name in commands if f"`agm {name}`" not in reference]
    assert not missing, f"commands missing from docs/cli.md: {missing}"


@pytest.mark.parametrize("document", DOCS, ids=lambda path: str(path.relative_to(ROOT)))
def test_relative_links_resolve(document: Path) -> None:
    text = FENCE.sub("", document.read_text(encoding="utf-8"))
    broken = []
    for target in LINK.findall(text):
        if re.match(r"^[a-z]+:", target):  # http(s), mailto
            continue
        file_part, _, anchor = target.partition("#")
        path = (document.parent / file_part).resolve() if file_part else document
        missing_anchor = bool(anchor) and path.suffix == ".md" and anchor not in _anchors(path)
        if not path.exists() or missing_anchor:
            broken.append(target)
    assert not broken, f"broken links in {document.name}: {broken}"


def test_slug_matches_github() -> None:
    assert _slug("Coleta automática com `agm run`") == "coleta-automática-com-agm-run"
    assert _slug("Validação externa com o dataset do *Agent READMEs*") == (
        "validação-externa-com-o-dataset-do-agent-readmes"
    )
    assert _slug("Piloto e alternativas (não usados nos resultados finais)") == (
        "piloto-e-alternativas-não-usados-nos-resultados-finais"
    )


def test_readme_stays_an_index() -> None:
    assert len((ROOT / "README.md").read_bytes()) < 10_000
