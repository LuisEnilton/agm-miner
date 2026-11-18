"""Figures of the paper (pure computations and the LaTeX of figuras.tex), from small frames."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from typer.testing import CliRunner

from agm_miner.analysis.paper_figures import (
    CUT_ALL,
    CUT_AR,
    CUT_ROOT,
    FIGURE_ELEMENTS,
    PREAMBLE,
    PREVALENCE_COLUMNS,
    Counts,
    FiguresInputError,
    category_counts,
    category_order,
    element_counts,
    figures_tex,
    percent_of,
    pipeline_numbers,
    prevalence_columns,
    root_artifact_ids,
    write_figures,
)
from agm_miner.cli import app

ROOT = Path(__file__).resolve().parents[1]
CODEBOOK = ROOT / "codebooks" / "seed_agent_readmes.yaml"


def structure_row(artifact_id: int, repo_id: int, path: str, **values: Any) -> dict[str, Any]:
    tool = "AGENTS" if path.endswith("AGENTS.md") else "CLAUDE"
    kind = "SKILL_MANIFEST" if path.endswith("SKILL.md") else "INSTRUCTION"
    base: dict[str, Any] = {
        "artifact_id": artifact_id,
        "repo_id": repo_id,
        "path": path,
        "tool": tool,
        "kind": kind,
        "scope": "NESTED" if "/" in path else "ROOT",
        "size_bytes": 3000,
        "is_symlink": False,
    }
    return base | {element: 0 for element in FIGURE_ELEMENTS} | values


STRUCTURE = pl.DataFrame(
    [
        structure_row(1, 1, "CLAUDE.md", tables=1, links=2),
        structure_row(2, 1, "pkg/CLAUDE.md", code_blocks=3),
        structure_row(3, 2, "CLAUDE.md", size_bytes=100),  # below the size limit
        structure_row(4, 2, "AGENTS.md", tables=1),
        structure_row(5, 3, "CLAUDE.md", is_symlink=True),
        structure_row(6, 3, ".claude/skills/a/SKILL.md"),
        # A skill resource has tool CLAUDE and may sit at the root: never a CLAUDE.md.
        structure_row(7, 4, "notes.md", kind="SKILL_RESOURCE"),
        structure_row(8, 4, "sub/CLAUDE.md", links=1),
    ]
)


def labels(*rows: tuple[int, str, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        [{"artifact_id": a, "tool": t, "kind": k, "category": c} for a, t, k, c in rows]
    )


OURS = labels(
    (1, "CLAUDE", "INSTRUCTION", "testing"),
    (1, "CLAUDE", "INSTRUCTION", "architecture"),
    (2, "CLAUDE", "INSTRUCTION", "testing"),
    (8, "CLAUDE", "INSTRUCTION", "other"),  # only "other": a file, with no category
    (4, "AGENTS", "INSTRUCTION", "security"),
    (6, "CLAUDE", "SKILL_MANIFEST", "testing"),
    (7, "CLAUDE", "SKILL_RESOURCE", "testing"),
)
# Section rows: a file has every category of its sections; COPILOT is not in the figure.
AGENTREADMES = labels(
    (1, "CLAUDE", "INSTRUCTION", "testing"),
    (1, "CLAUDE", "INSTRUCTION", "testing"),
    (1, "CLAUDE", "INSTRUCTION", "security"),
    (2, "CLAUDE", "INSTRUCTION", "architecture"),
    (3, "AGENTS", "INSTRUCTION", "testing"),
    (4, "COPILOT", "INSTRUCTION", "testing"),
)
AUDIT: dict[str, Any] = {
    "code_version": "abc",
    "study_config": {"analysis": {"min_human_contributors": 2}},
    "selection": {
        "repositories": 286019,
        "evaluated": 284656,
        "kept": 5946,
        "reference_date": "2026-09-23",
    },
    "mining": {"mined": 5934},
}
NAMES = {"testing": "Testing", "architecture": "Architecture", "security": "Security"}
CATEGORIES = ["architecture", "security", "testing"]


def test_percentages_are_rounded_once_from_the_exact_counts() -> None:
    # The paper's table once printed 26: the share rounded to 0.255, then to a percentage.
    assert percent_of(2031, 7977) == 25
    assert round(round(2031 / 7977, 3) * 100 + 1e-9) == 26
    assert percent_of(5552, 7989) == 69  # 69.50% -> 0.695 -> 70 when rounded twice
    assert percent_of(1, 8) == 13 and percent_of(1, 2) == 50  # halves go up
    assert percent_of(0, 5) == 0 and percent_of(5, 5) == 100
    with pytest.raises(ValueError):
        percent_of(1, 0)
    assert Counts(7977, {"x": 2031}).percent("x") == 25 and Counts(3).percent("x") == 0


def test_category_counts_per_file_type() -> None:
    counts = category_counts(OURS)
    assert set(counts) == {"CLAUDE.md", "AGENTS.md", "SKILL.md"}  # the resource is dropped
    claude = counts["CLAUDE.md"]
    assert claude.files == 3 and dict(claude.having) == {"testing": 2, "architecture": 1}
    assert "other" not in claude.having
    assert counts["SKILL.md"].files == 1
    # Several sections of a file with the same category count the file once.
    ar = category_counts(AGENTREADMES)
    assert set(ar) == {"CLAUDE.md", "AGENTS.md"}
    assert ar["CLAUDE.md"].files == 2 and ar["CLAUDE.md"].having["testing"] == 1


def test_root_files_skip_resources_small_files_and_symlinks() -> None:
    roots = set(root_artifact_ids(STRUCTURE, 2048).to_list())
    # 3 is too small, 5 a symlink, 7 a skill resource; nested files are never root files.
    assert roots == {1, 4}


def test_prevalence_columns() -> None:
    columns = prevalence_columns(OURS, STRUCTURE, AGENTREADMES, 2048)
    assert list(columns) == list(PREVALENCE_COLUMNS)
    assert columns[("CLAUDE.md", CUT_ALL)].files == 3
    root = columns[("CLAUDE.md", CUT_ROOT)]
    assert root.files == 1 and root.percent("testing") == 100 and root.percent("security") == 0
    assert columns[("AGENTS.md", CUT_ROOT)].percent("security") == 100
    assert columns[("CLAUDE.md", CUT_AR)].percent("testing") == 50
    assert columns[("AGENTS.md", CUT_AR)].files == 1
    # Testing (2/3) before architecture (1/3); security (0) last.
    assert category_order(columns, CATEGORIES) == ["testing", "architecture", "security"]
    with pytest.raises(ValueError, match="AGENTS.md files in the AR column"):
        prevalence_columns(OURS, STRUCTURE, AGENTREADMES.filter(pl.col("tool") != "AGENTS"), 0)


def test_element_counts_use_the_rq2_files() -> None:
    counts = element_counts(STRUCTURE, 2048)
    claude = counts["CLAUDE.md"]
    assert claude.files == 3  # 1, 2 and 8
    assert claude.having == {
        "tables": 1,
        "code_blocks": 1,
        "ordered_lists": 0,
        "bullet_lists": 0,
        "links": 2,
    }
    assert claude.percent("links") == 67 and counts["AGENTS.md"].percent("tables") == 100


def test_pipeline_numbers() -> None:
    numbers = pipeline_numbers(AUDIT, STRUCTURE, 2048)
    assert (numbers.discovered, numbers.evaluated, numbers.selected, numbers.mined) == (
        286019,
        284656,
        5946,
        5934,
    )
    # CLAUDE.md 1, 2, 8; AGENTS.md 4; SKILL.md 6.
    assert numbers.sample_files == 5 and numbers.sample_repositories == 4


def _tex() -> str:
    return figures_tex(
        pipeline_numbers(AUDIT, STRUCTURE, 2048),
        (element_counts(STRUCTURE, 2048), element_counts(STRUCTURE, 2048)),
        2048,
        prevalence_columns(OURS, STRUCTURE, AGENTREADMES, 2048),
        CATEGORIES,
        NAMES | {"security": "Security & Privacy"},
        {"Analysis": "data/gold/x"},
    )


def test_figures_tex_defines_one_figure_macro_each() -> None:
    tex = _tex()
    defined = re.findall(r"\\newcommand\{\\(\w+)\}", tex)
    assert defined == ["figPipeline", "figElementos", "figPrevalencia"]
    for label in ("fig:pipeline", "fig:elementos", "fig:prevalencia"):
        assert tex.count(f"\\label{{{label}}}") == 1
    assert tex.count("\\begin{figure}") == 3 and tex.count("\\end{figure}") == 3
    # The preamble the paper needs is stated at the top of the file.
    header = tex[: tex.index("\\newcommand")]
    assert all(f"%   {line}" in header for line in PREAMBLE)
    # Brazilian numbers; captions without \cite (it breaks the SBC caption spacing).
    assert "286.019 repositórios" in tex and "5.946 selecionados" in tex
    assert "23/09/2026" in tex and "\\cite" not in tex
    assert "Security \\& Privacy" in tex
    # A "#" inside \newcommand is read as a parameter: none may appear.
    assert "#" not in tex


def test_heatmap_prints_each_percentage_once_rounded() -> None:
    columns = {key: Counts(7977, {"testing": 2031}) for key in PREVALENCE_COLUMNS}
    tex = figures_tex(
        pipeline_numbers(AUDIT, STRUCTURE, 2048),
        (element_counts(STRUCTURE, 2048), element_counts(STRUCTURE, 2048)),
        2048,
        columns,
        ["testing"],
        NAMES,
        {},
    )
    heatmap = tex[tex.index("\\figPrevalencia") :]
    cells = re.findall(r"\\node\[row, text=\w+\] at \([\d.,-]+\) \{(\d+)\};", heatmap)
    assert cells == ["25"] * len(PREVALENCE_COLUMNS)
    assert "$n$=7.977" in heatmap


def _write_inputs(data_dir: Path) -> tuple[Path, Path]:
    gold = data_dir / "gold" / "20261117T000000Z"
    compare = data_dir / "external" / "agentreadmes"
    audit = data_dir / "audit" / gold.name
    for directory in (gold, compare, audit):
        directory.mkdir(parents=True)
    STRUCTURE.write_parquet(gold / "rq2_structure.parquet")
    STRUCTURE.write_parquet(compare / "rq2_structure.parquet")
    manifest = {"code_version": "abc", "analysis_config": {"min_artifact_bytes": 2048}}
    (gold / "analysis_manifest.json").write_text(json.dumps(manifest))
    (audit / "audit.json").write_text(json.dumps(AUDIT))
    model = Path("classify", "agent_readmes_2025", "anthropic", "claude-sonnet-5-5")
    ours, theirs = data_dir / "rq1" / gold.name / model, data_dir / "rq1" / "agentreadmes" / model
    for directory in (ours, theirs):
        directory.mkdir(parents=True)
    OURS.write_parquet(ours / "artifact_labels.parquet")
    AGENTREADMES.write_parquet(theirs / "section_labels.parquet")
    return gold, ours


def test_write_figures_and_command(tmp_path: Path, monkeypatch) -> None:
    gold, ours = _write_inputs(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    done = CliRunner().invoke(
        app,
        ["figures", "--model", "anthropic:claude-sonnet-5-5", "--codebook", str(CODEBOOK)],
    )
    assert done.exit_code == 0, done.output
    result = json.loads(done.output[done.output.index("{") :])
    output = tmp_path / "figures" / gold.name / "figuras.tex"
    assert result["output"] == str(output) and output.exists()
    assert not output.with_name("figuras.tex.partial").exists()
    assert result["prevalence"]["CLAUDE.md Todos"]["testing"] == 67
    assert result["pipeline"]["sample_files"] == 5
    assert "\\newcommand{\\figPrevalencia}" in output.read_text()

    (ours / "artifact_labels.parquet").unlink()
    with pytest.raises(FiguresInputError, match="artifact_labels"):
        write_figures(
            gold_dir=gold,
            audit_dir=tmp_path / "audit" / gold.name,
            compare_dir=tmp_path / "external" / "agentreadmes",
            labels_dir=ours,
            compare_labels_dir=ours,
            output_dir=tmp_path / "out",
            categories=CATEGORIES,
            names=NAMES,
        )
    done = CliRunner().invoke(
        app,
        ["figures", "--model", "anthropic:claude-sonnet-5-5", "--codebook", str(CODEBOOK)],
    )
    assert done.exit_code == 1 and "artifact_labels" in done.output


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


@pytest.mark.skipif(
    not _has("pdflatex")
    or not _has("kpsewhich")
    or not subprocess.run(["kpsewhich", "pgfplots.sty"], capture_output=True).stdout,
    reason="pdflatex with pgfplots is not installed",
)
def test_generated_figures_compile(tmp_path: Path) -> None:
    (tmp_path / "figuras.tex").write_text(_tex(), encoding="utf-8")
    preamble = "\n".join(PREAMBLE)
    (tmp_path / "paper.tex").write_text(
        "\\documentclass[12pt]{article}\n\\usepackage[utf8]{inputenc}\n"
        "\\usepackage[T1]{fontenc}\n\\usepackage[brazil]{babel}\n"
        "\\usepackage[a4paper,left=3cm,right=3cm]{geometry}\n"
        f"{preamble}\n\\input{{figuras}}\n\\begin{{document}}\n"
        "\\figPipeline\n\\figElementos\n\\figPrevalencia\n\\end{document}\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "paper.tex"],
        cwd=tmp_path,
        capture_output=True,
        timeout=180,
    )
    log = done.stdout.decode("latin-1")
    assert done.returncode == 0, log[-2000:]
    # Every figure fits the 15 cm of text of the SBC template.
    assert "Overfull \\hbox" not in log
