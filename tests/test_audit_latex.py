"""LaTeX output of the sampling audit (macros, tables and draft for the paper)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agm_miner.analysis.audit_latex import (
    audit_tex,
    camel,
    day,
    draft_tex,
    escape,
    macro_values,
    number,
    percent,
    top_criterion,
)
from agm_miner.analysis.sampling_audit import _warnings, audit_tables
from tests.test_sampling_audit import STUDY, bronze_tables

MACRO_USE = re.compile(r"\\(audit[A-Za-z]+)")
MACRO_DEFINITION = re.compile(r"\\newcommand\{\\(audit[A-Za-z]+)\}")


def make_audit(**changes: Any) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "bronze_dir": "data/bronze/20261116T000000Z",
        "exported_at": "2026-11-16T10:00:00+00:00",
        "code_version": "abc",
        "study_config_hash": "hash",
        "config_differs_from_export": False,
        "study_config": STUDY.snapshot(),
        **audit_tables(bronze_tables(), STUDY),
    }
    audit |= changes
    audit["warnings"] = _warnings(audit)
    return audit


def test_formatting() -> None:
    assert number(5380) == "5.380" and number(1234567.0) == "1.234.567" and number(None) == "---"
    assert percent(0.55) == r"55\%" and percent(0.095) == r"9,5\%" and percent(0) == r"0\%"
    assert percent(0.00002) == r"$<$0,1\%" and percent(None) == "---"
    assert day("2026-09-23") == "23/09/2026" and day("2026-10-01T12:00:00+00:00") == "01/10/2026"
    assert day(None) == "---"
    assert camel("created_after_cutoff") == "CreatedAfterCutoff"
    assert escape("a_b & 50% #1 {x}") == r"a\_b \& 50\% \#1 \{x\}"


def test_macro_values() -> None:
    values = macro_values(make_audit())
    assert values["auditKept"] == "3" and values["auditEvaluated"] == "5"
    assert values["auditCoverageClaude"] == r"70\%" and values["auditCoverageAgents"] == "---"
    assert values["auditOnlyLicense"] == "1" and values["auditFailShareLicense"] == r"20\%"
    assert values["auditKeptOutsideShare"] == r"33\%"
    assert values["auditLicenseExamples"] == r"\texttt{org/r3}"
    assert values["auditAboveCollectedShareClaude"] == r"33\%"
    assert values["auditTopOnePctShare"] == r"60\%"
    assert values["auditReferenceDate"] == "23/09/2026"
    assert values["auditSizeMin"] == "100"
    # Every kept repository of the fixture was created in 2020, before the related cutoff.
    assert values["auditKeptSinceRelatedCutoffShare"] == r"0\%"


def test_repository_names_are_escaped() -> None:
    audit = make_audit()
    audit["selection"]["only_license"]["top_by_stars"][0]["full_name"] = "my_org/a_b"
    assert macro_values(audit)["auditLicenseExamples"] == r"\texttt{my\_org/a\_b}"


def test_audit_tex_defines_every_macro_the_draft_uses() -> None:
    audit = make_audit()
    tex, draft = audit_tex(audit), draft_tex(audit)
    defined = MACRO_DEFINITION.findall(tex)
    assert len(defined) == len(set(defined))
    assert {"auditTableSelection", "auditTableDiscovery", "auditTableArtifacts"} <= set(defined)
    assert set(MACRO_USE.findall(draft)) <= set(defined)
    assert tex.count("\\begin{table}") == 3 and tex.count("{") == tex.count("}")
    assert "\\label{tab:audit-selection}" in tex
    # Only criteria that reject something get a row.
    assert "licença não aprovada pela OSI & 1 & 20\\% & 1 \\\\" in tex
    assert "repositório vazio" not in tex
    assert "\\texttt{CLAUDE.md} & 3.000 & 2.100 & 70\\% & 100--550 & 200 & 6 \\\\" in tex


def test_draft_follows_the_findings() -> None:
    incomplete = draft_tex(make_audit())
    assert "% TODO: the discovery is incomplete" in incomplete
    assert "ficaram inacessíveis" in incomplete
    assert "%   - query 'filename:AGENTS.md' was never searched" in incomplete
    audit = make_audit()
    for query in audit["discovery"]["queries"].values():
        query |= {"complete": True, "unreachable_matches": 0}
    complete = draft_tex(audit)
    assert "TODO" not in complete and "Nenhuma faixa ultrapassou o limite" in complete


def test_draft_describes_the_history_cut() -> None:
    audit = make_audit()
    values = macro_values(audit)
    assert values["auditCutoffCommits"] == "1" and values["auditCutoffNotYetCreated"] == "1"
    assert values["auditEligibleAtHead"] == "5" and values["auditEligibleAtCutoff"] == "4"
    assert values["auditMaxDaysMissing"] == "2,0"
    cut = draft_tex(audit)
    assert "foram descartados \\auditCutoffCommits{} commits" in cut
    assert "\\auditMinedBeforeCutoff{} repositórios minerados antes" in cut
    assert "O histórico de cada repositório foi cortado" in cut
    audit["history"]["enabled"] = False
    uncut = draft_tex(audit)
    assert "têm histórico posterior a ela" in uncut and "foram descartados" not in uncut
    audit["history"] = {"enabled": False, "reference_date": None}
    assert macro_values(audit)["auditCutoffCommits"] == "---"


@pytest.mark.skipif(shutil.which("pdflatex") is None, reason="pdflatex is not installed")
def test_generated_latex_compiles(tmp_path: Path) -> None:
    audit = make_audit()
    (tmp_path / "audit.tex").write_text(audit_tex(audit), encoding="utf-8")
    (tmp_path / "audit_rascunho.tex").write_text(draft_tex(audit), encoding="utf-8")
    (tmp_path / "paper.tex").write_text(
        "\\documentclass[12pt]{article}\n\\usepackage[utf8]{inputenc}\n"
        "\\usepackage[T1]{fontenc}\n\\input{audit}\n\\begin{document}\n"
        "\\input{audit_rascunho}\n\\auditTableDiscovery\n\\auditTableSelection\n"
        "\\auditTableArtifacts\n\\end{document}\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "paper.tex"],
        cwd=tmp_path,
        capture_output=True,
        timeout=120,
    )
    # pdflatex logs in 8-bit, not UTF-8.
    assert done.returncode == 0, done.stdout.decode("latin-1")[-2000:]
    assert (tmp_path / "paper.pdf").exists()


def test_the_draft_names_the_criterion_that_rejects_the_most() -> None:
    audit = make_audit()
    # License and stars reject one repository each: the tie goes to the evaluation order.
    assert top_criterion(audit["selection"]) == "license"
    values = macro_values(audit)
    assert values["auditTopCriterion"] == "licença não aprovada pela OSI"
    assert values["auditTopCriterionShare"] == r"20\%"
    draft = draft_tex(audit)
    assert "O critério que mais exclui repositórios é \\emph{\\auditTopCriterion}" in draft
    assert "mais restritivo" not in draft

    audit["selection"]["criteria"]["min_stars"]["fail"] = 4
    audit["selection"]["criteria"]["min_stars"]["fail_share"] = 0.8
    assert top_criterion(audit["selection"]) == "min_stars"
    assert macro_values(audit)["auditTopCriterion"] == "menos de 50 estrelas"

    for effect in audit["selection"]["criteria"].values():
        effect["fail"] = 0
    assert top_criterion(audit["selection"]) is None
    assert macro_values(audit)["auditTopCriterion"] == "---"
    assert "mais exclui" not in draft_tex(audit)
