"""Figures of the paper as pure LaTeX (TikZ and pgfplots), regenerated from the data.

``figuras.tex`` defines one macro per figure, each expanding to a complete ``figure`` environment
with caption and label, in Portuguese and in gray tones (SBC template):

* ``\\figPipeline`` (``fig:pipeline``): the stages of the pipeline with the number of
  repositories and files at each one, from ``audit.json`` and ``rq2_structure``;
* ``\\figElementos`` (``fig:elementos``): share of files with each Markdown element (QP2), ours
  next to the *Agent READMEs* files, from the ``rq2_structure`` of both analysis outputs;
* ``\\figPrevalencia`` (``fig:prevalencia``): share of files with each content category (QP1), as
  a heatmap, from the ``classify`` outputs of the RQ1 (ours and the *Agent READMEs* files).

Like ``audit.tex`` (``analysis.audit_latex``), the paper loads the file once in the preamble and
places each figure where it wants; a new analysis only replaces the file. Every number is
computed here, and every percentage is rounded once, from the exact counts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from agm_miner.analysis.audit_latex import day, escape, number
from agm_miner.analysis.datasets import ANALYSIS_MANIFEST
from agm_miner.analysis.rq2_summary import RQ2_STRUCTURE, file_types, one_per_repository, prepare
from agm_miner.rq1.codebook import OTHER

FIGURES_TEX = "figuras.tex"
AUDIT_JSON = "audit.json"
ARTIFACT_LABELS, SECTION_LABELS = "artifact_labels.parquet", "section_labels.parquet"
# What the preamble needs: pgfplots loads TikZ, and the figures use no TikZ library.
PREAMBLE = (r"\usepackage{pgfplots}", r"\pgfplotsset{compat=1.18}")

INSTRUCTION_TYPES = ("CLAUDE.md", "AGENTS.md")
# Elements of the QP2 figure, in display order (columns of rq2_structure -> label).
FIGURE_ELEMENTS: dict[str, str] = {
    "tables": "Tabelas",
    "code_blocks": "Blocos de código",
    "ordered_lists": "Listas numeradas",
    "bullet_lists": "Listas com marcadores",
    "links": "Links",
}
# Columns of the QP1 heatmap: (file type, cut). Todos: every classified file; Raiz: the root file
# of each repository; AR: the Agent READMEs files, classified by the same model.
CUT_ALL, CUT_ROOT, CUT_AR = "Todos", "Raiz", "AR"
PREVALENCE_COLUMNS = (
    ("CLAUDE.md", CUT_ALL),
    ("CLAUDE.md", CUT_ROOT),
    ("CLAUDE.md", CUT_AR),
    ("AGENTS.md", CUT_ALL),
    ("AGENTS.md", CUT_ROOT),
    ("AGENTS.md", CUT_AR),
    ("SKILL.md", CUT_ALL),
)
_STRUCTURE_COLUMNS = ["artifact_id", "repo_id", "path", "tool", "kind", "scope", "size_bytes"]
_LABEL_COLUMNS = ["artifact_id", "tool", "kind", "category"]


class FiguresInputError(FileNotFoundError):
    """An input of ``agm figures`` is missing."""


# --------------------------------------------------------------------------- numbers


def percent_of(part: int, whole: int) -> int:
    """``part / whole`` as a whole percentage, rounded once (half up) from the exact counts:
    2031/7977 = 25.46% is 25, never 26 (as rounding a share already rounded to 0.255 would)."""
    if whole <= 0:
        raise ValueError("percentage of an empty group")
    return (200 * part + whole) // (2 * whole)


@dataclass(frozen=True, slots=True)
class Counts:
    """Files of one group and how many of them have each item (element or category)."""

    files: int
    having: Mapping[str, int] = field(default_factory=dict)

    def percent(self, item: str) -> int:
        return percent_of(self.having.get(item, 0), self.files)

    def share(self, item: str) -> float:
        return self.having.get(item, 0) / self.files if self.files else 0.0


def element_counts(
    structure: pl.DataFrame, min_bytes: int, elements: Sequence[str] = tuple(FIGURE_ELEMENTS)
) -> dict[str, Counts]:
    """Per file type, the files of ``rq2_structure`` kept by the RQ2 summary (``prepare``) and how
    many have each element (the ``all/files`` cut of ``agm rq2 summary``)."""
    files = prepare(structure, min_bytes)
    result = {}
    for file_type, group in files.group_by("file_type", maintain_order=True):
        having = {e: int((group[e].fill_null(0) > 0).sum()) for e in elements}
        result[str(file_type[0])] = Counts(group.height, having)
    return result


def category_counts(labels: pl.DataFrame) -> dict[str, Counts]:
    """Per file type, the files with at least one classified section and how many have each
    category. ``labels`` has one row per section or per file and category (``artifact_id``,
    ``tool``, ``kind``, ``category``); a file has every category of its sections. ``other`` is
    not a category, but a file with only ``other`` sections still counts as a file."""
    typed = file_types(labels).unique(["artifact_id", "file_type", "category"])
    result = {}
    for file_type, group in typed.group_by("file_type", maintain_order=True):
        categories = group.filter(pl.col("category") != OTHER)["category"].value_counts()
        having = dict(zip(categories["category"], categories["count"], strict=True))
        result[str(file_type[0])] = Counts(group["artifact_id"].n_unique(), having)
    return result


def root_artifact_ids(structure: pl.DataFrame, min_bytes: int) -> pl.Series:
    """The root file of each repository and file type, among the files of the RQ2 summary
    (``prepare`` drops skill resources, which have tool CLAUDE)."""
    files = prepare(structure, min_bytes).filter(pl.col("scope") == "ROOT")
    return one_per_repository(files)["artifact_id"]


def prevalence_columns(
    ours: pl.DataFrame,
    structure: pl.DataFrame,
    agentreadmes: pl.DataFrame,
    min_bytes: int,
    compared_structure: pl.DataFrame,
) -> dict[tuple[str, str], Counts]:
    """The columns of the QP1 heatmap (``PREVALENCE_COLUMNS``): our classified files (Todos),
    the root file of each repository among them (Raiz) and the Agent READMEs files (AR, CLAUDE.md
    and AGENTS.md only). The AR files go through the same size filter as ours (their
    ``compared_structure``), since the share of most categories grows with the file size."""
    roots = root_artifact_ids(structure, min_bytes)
    kept = prepare(compared_structure, min_bytes)["artifact_id"]
    cuts = {
        CUT_ALL: category_counts(ours),
        CUT_ROOT: category_counts(ours.filter(pl.col("artifact_id").is_in(roots.implode()))),
        CUT_AR: category_counts(agentreadmes.filter(pl.col("artifact_id").is_in(kept.implode()))),
    }
    columns = {}
    for file_type, cut in PREVALENCE_COLUMNS:
        counts = cuts[cut].get(file_type)
        if counts is None or not counts.files:
            raise ValueError(f"no classified {file_type} files in the {cut} column")
        columns[(file_type, cut)] = counts
    return columns


def category_order(
    columns: Mapping[tuple[str, str], Counts], categories: Sequence[str]
) -> list[str]:
    """Categories sorted by the exact share of the first column (most frequent first), ties by
    the given order."""
    first = columns[PREVALENCE_COLUMNS[0]]
    position = {category: index for index, category in enumerate(categories)}
    return sorted(categories, key=lambda c: (-first.share(c), position[c]))


@dataclass(frozen=True, slots=True)
class PipelineNumbers:
    discovered: int
    evaluated: int
    selected: int
    mined: int
    sample_files: int
    sample_repositories: int
    reference_date: str
    min_bytes: int
    min_humans: int


def pipeline_numbers(
    audit: Mapping[str, Any], structure: pl.DataFrame, min_bytes: int
) -> PipelineNumbers:
    """Counts of each stage: discovery, selection and mining from the audit; the analysed sample
    (CLAUDE.md, AGENTS.md and SKILL.md files kept by the RQ2 summary) from ``rq2_structure``."""
    config = audit["study_config"]
    selection, mining = audit["selection"], audit["mining"]
    sample = prepare(structure, min_bytes)
    return PipelineNumbers(
        discovered=selection["repositories"],
        evaluated=selection["evaluated"],
        selected=selection["kept"],
        mined=mining["mined"],
        sample_files=sample.height,
        sample_repositories=sample["repo_id"].n_unique(),
        reference_date=selection["reference_date"],
        min_bytes=min_bytes,
        min_humans=config["analysis"]["min_human_contributors"],
    )


# --------------------------------------------------------------------------- LaTeX


def _figure(body: str, caption: str, label: str) -> str:
    return (
        "\\begin{figure}[ht]\n"
        "  \\centering\n"
        f"{body}\n"
        f"  \\caption{{{caption}}}\n"
        f"  \\label{{{label}}}\n"
        "\\end{figure}"
    )


# Pipeline layout (cm): three columns, two rows of boxes with the same height, so the arrows and
# the second row never depend on how many lines a box has.
_COLUMNS = (2.1, 7.3, 12.5)
_ROW_HEIGHT, _ROW_GAP = 2.2, 0.6


def pipeline_figure(numbers: PipelineNumbers) -> str:
    """Flowchart, read as a snake: Descoberta, Seleção and Mineração on the first row, then
    Exportação and Análise from right to left; Análise branches into the two research
    questions."""
    n = numbers
    box = (
        "draw, rounded corners=2pt, align=center, font=\\footnotesize, inner sep=3pt, "
        f"text width=3.9cm, minimum height={_ROW_HEIGHT}cm, anchor=north"
    )
    second = -(_ROW_HEIGHT + _ROW_GAP)
    stages = [
        (
            "disc",
            (_COLUMNS[0], 0),
            "Descoberta",
            "busca de código do\\\\GitHub, por faixas de\\\\tamanho de arquivo",
            f"{number(n.discovered)} repositórios",
        ),
        (
            "sel",
            (_COLUMNS[1], 0),
            "Seleção",
            "metadados via GraphQL\\\\e critérios de seleção",
            f"{number(n.evaluated)} avaliados,\\\\{number(n.selected)} selecionados",
        ),
        (
            "mine",
            (_COLUMNS[2], 0),
            "Mineração",
            f"clone parcial; histórico\\\\cortado em {day(n.reference_date)}",
            f"{number(n.mined)} minerados",
        ),
        ("exp", (_COLUMNS[2], second), "Exportação", "dados brutos\\\\(Parquet)", ""),
        (
            "ana",
            (_COLUMNS[1], second),
            "Análise",
            f"arquivos com $\\geq$ {number(n.min_bytes)}\\\\bytes, repositórios com\\\\"
            f"$\\geq$ {number(n.min_humans)} autores humanos",
            f"{number(n.sample_files)} arquivos de\\\\{number(n.sample_repositories)} repositórios",
        ),
    ]
    lines = [f"  \\begin{{tikzpicture}}[box/.style={{{box}}}, >=stealth, thick]"]
    for name, (x, y), title, text, counted in stages:
        content = f"\\textbf{{{title}}}\\\\{text}"
        if counted:
            content += f"\\\\\\textit{{{counted}}}"
        lines.append(f"    \\node[box] ({name}) at ({x},{y}) {{{content}}};")
    question = "box, minimum height=1.25cm, fill=black!8"
    lines += [
        f"    \\node[{question}] (qp1) at ({_COLUMNS[0]},{second + 0.15}) "
        "{\\textbf{QP1}: categorias de cada\\\\seção com LLM, validadas\\\\"
        "com o \\textit{Agent READMEs}};",
        f"    \\node[{question}] (qp2) at ([yshift=-0.3cm]qp1.south) "
        "{\\textbf{QP2}: métricas de\\\\estrutura com\\\\parser Markdown};",
    ]
    lines += [
        "    \\draw[->] (disc) -- (sel);",
        "    \\draw[->] (sel) -- (mine);",
        "    \\draw[->] (mine) -- (exp);",
        "    \\draw[->] (exp.west) -- (exp.west -| ana.east);",
        "    \\draw[->] (ana.west) -- ++(-0.3,0) |- (qp1.east);",
        "    \\draw[->] (ana.west) -- ++(-0.3,0) |- (qp2.east);",
        "  \\end{tikzpicture}",
    ]
    return _figure(
        "\n".join(lines),
        "Etapas da coleta e da análise, com o número de repositórios e de arquivos em cada "
        "uma. A análise usa os arquivos \\texttt{CLAUDE.md}, \\texttt{AGENTS.md} e "
        "\\texttt{SKILL.md} dos repositórios da amostra.",
        "fig:pipeline",
    )


BAR_PANEL_WIDTH, BAR_PANEL_GAP = 4.6, 0.9  # cm: plot area of each panel of the QP2 figure


def _bar_axis(file_type: str, ours: Counts, theirs: Counts, *, first: bool) -> list[str]:
    symbolic = ",".join(FIGURE_ELEMENTS)
    options = [
        "xbar, bar width=5pt, y dir=reverse, scale only axis",
        f"width={BAR_PANEL_WIDTH}cm, height=4.2cm",
        "xmin=0, xmax=118, xtick={0,25,50,75,100}, axis x line*=bottom, axis y line*=left",
        f"title={{\\texttt{{{file_type}}}\\\\\\scriptsize nosso: {number(ours.files)} "
        f"arquivos; AR: {number(theirs.files)}}}, title style={{align=center, "
        "font=\\footnotesize}",
        f"symbolic y coords={{{symbolic}}}, ytick=data, enlarge y limits=0.14",
        "ticklabel style={font=\\scriptsize}",
        "nodes near coords, nodes near coords style={font=\\tiny}",
    ]
    if first:
        labels = ",".join(f"{{{label}}}" for label in FIGURE_ELEMENTS.values())
        options.append(f"yticklabels={{{labels}}}")
    else:
        # The legend sits under the gap between the two panels.
        middle = -BAR_PANEL_GAP / 2 / BAR_PANEL_WIDTH
        options += [
            "yticklabels={}",
            f"at={{(first.south east)}}, anchor=south west, xshift={BAR_PANEL_GAP}cm",
            "area legend, reverse legend, legend columns=2, legend style={font=\\scriptsize, "
            f"at={{({middle:.3f},-0.14)}}, anchor=north, draw=none}}",
        ]
    name = "first" if first else "second"
    lines = [f"    \\begin{{axis}}[name={name},", *(f"      {o}," for o in options), "    ]"]
    # With y dir=reverse the last plot is drawn on top of each group: ours goes last.
    for style, counts in (("fill=white, draw=black", theirs), ("fill=black!65, draw=black", ours)):
        points = " ".join(f"({counts.percent(e)},{e})" for e in FIGURE_ELEMENTS)
        lines.append(f"      \\addplot[{style}] coordinates {{{points}}};")
    if not first:
        lines.append("      \\legend{AR, Nosso}")
    lines.append("    \\end{axis}")
    return lines


def elements_figure(
    ours: Mapping[str, Counts], theirs: Mapping[str, Counts], min_bytes: int
) -> str:
    """Horizontal grouped bars: share of files with each element, ours and AR, one panel per
    file type (the element names only on the left panel)."""
    lines = ["  \\begin{tikzpicture}"]
    for index, file_type in enumerate(INSTRUCTION_TYPES):
        lines += _bar_axis(file_type, ours[file_type], theirs[file_type], first=index == 0)
    lines.append("  \\end{tikzpicture}")
    return _figure(
        "\n".join(lines),
        "Porcentagem dos arquivos com cada elemento de Markdown, na nossa amostra (todos os "
        "arquivos) e nos arquivos do \\textit{Agent READMEs} (AR), nos dois casos com pelo "
        f"menos {number(min_bytes)} bytes.",
        "fig:elementos",
    )


CELL_WIDTH, CELL_HEIGHT, NAME_WIDTH = 1.4, 0.42, 4.4


def _cell(column: int, row: int, value: int) -> str:
    x0, y0 = NAME_WIDTH + column * CELL_WIDTH, -(row + 1) * CELL_HEIGHT
    gray = round(0.75 * value)
    color = "white" if gray >= 40 else "black"
    return (
        f"    \\fill[black!{gray}] ({x0:.2f},{y0:.2f}) rectangle "
        f"++({CELL_WIDTH},{CELL_HEIGHT});\n"
        f"    \\node[row, text={color}] at ({x0 + CELL_WIDTH / 2:.3f},{y0 + CELL_HEIGHT / 2:.2f}) "
        f"{{{value}}};"
    )


def prevalence_figure(
    columns: Mapping[tuple[str, str], Counts],
    categories: Sequence[str],
    names: Mapping[str, str],
    min_bytes: int,
) -> str:
    """Heatmap of the share of files with each category (rows) per file type and cut (columns),
    with the whole percentage in each cell (darker: more files)."""
    order = category_order(columns, categories)
    keys = list(PREVALENCE_COLUMNS)
    width = NAME_WIDTH + len(keys) * CELL_WIDTH
    # Same height and depth for every row label and cell, so the text of a row lines up.
    lines = [
        "  \\begin{tikzpicture}[font=\\footnotesize, "
        "row/.style={text height=1.6ex, text depth=0.4ex}]"
    ]
    # Header: file type over its columns, then the cut and the number of files.
    groups: dict[str, list[int]] = {}
    for index, (file_type, _) in enumerate(keys):
        groups.setdefault(file_type, []).append(index)
    for file_type, indexes in groups.items():
        left = NAME_WIDTH + indexes[0] * CELL_WIDTH + 0.05
        right = NAME_WIDTH + (indexes[-1] + 1) * CELL_WIDTH - 0.05
        lines.append(f"    \\node at ({(left + right) / 2:.3f},1.05) {{\\texttt{{{file_type}}}}};")
        if len(indexes) > 1:  # a rule over the columns of the group
            lines.append(f"    \\draw ({left:.2f},0.85) -- ({right:.2f},0.85);")
    for index, key in enumerate(keys):
        x = NAME_WIDTH + (index + 0.5) * CELL_WIDTH
        lines.append(
            f"    \\node[align=center, font=\\scriptsize] at ({x:.3f},0.42) "
            f"{{{key[1]}\\\\$n$={number(columns[key].files)}}};"
        )
    for row, category in enumerate(order):
        y = -(row + 0.5) * CELL_HEIGHT
        lines.append(
            f"    \\node[row, anchor=east] at ({NAME_WIDTH - 0.1:.2f},{y:.2f}) "
            f"{{{escape(names.get(category, category))}}};"
        )
        lines += [
            _cell(column, row, columns[key].percent(category)) for column, key in enumerate(keys)
        ]
    bottom = -len(order) * CELL_HEIGHT
    for index in (3, 6):  # separators between the file types
        x = NAME_WIDTH + index * CELL_WIDTH
        lines.append(f"    \\draw[white, line width=1.5pt] ({x:.2f},0) -- ({x:.2f},{bottom:.2f});")
    lines.append(f"    \\useasboundingbox (0,{bottom:.2f}) rectangle ({width:.2f},1.3);")
    lines.append("  \\end{tikzpicture}")
    return _figure(
        "\n".join(lines),
        "Porcentagem dos arquivos com cada categoria de conteúdo. \\emph{Todos}: arquivos com "
        "ao menos uma seção classificada; \\emph{Raiz}: o arquivo da raiz de cada repositório; "
        f"AR: arquivos rotulados do \\textit{{Agent READMEs}} com pelo menos {number(min_bytes)} "
        "bytes, "
        "classificados pelo mesmo modelo. Categorias "
        "ordenadas pela coluna \\emph{Todos} de \\texttt{CLAUDE.md}.",
        "fig:prevalencia",
    )


def figures_tex(
    pipeline: PipelineNumbers,
    elements: tuple[Mapping[str, Counts], Mapping[str, Counts]],
    min_bytes: int,
    prevalence: Mapping[tuple[str, str], Counts],
    categories: Sequence[str],
    names: Mapping[str, str],
    sources: Mapping[str, Any],
) -> str:
    """The whole ``figuras.tex``: provenance and preamble comments, then one macro per figure."""
    lines = ["% Generated by `agm figures`: do not edit, run the command again."]
    lines += [f"% {name}: {value}" for name, value in sources.items()]
    lines += ["% Preamble of the paper (before \\input{figuras}):"]
    lines += [f"%   {line}" for line in PREAMBLE]
    lines.append("")
    for name, body in (
        ("figPipeline", pipeline_figure(pipeline)),
        ("figElementos", elements_figure(*elements, min_bytes)),
        ("figPrevalencia", prevalence_figure(prevalence, categories, names, min_bytes)),
    ):
        lines += [f"\\newcommand{{\\{name}}}{{%", body, "}", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- files


def _require(path: Path) -> Path:
    if not path.exists():
        raise FiguresInputError(f"No {path.name} in {path.parent}")
    return path


def _structure(directory: Path) -> pl.DataFrame:
    return pl.read_parquet(
        _require(directory / RQ2_STRUCTURE),
        columns=[*_STRUCTURE_COLUMNS, "is_symlink", *FIGURE_ELEMENTS],
    )


def write_figures(
    *,
    gold_dir: Path,
    audit_dir: Path,
    compare_dir: Path,
    labels_dir: Path,
    compare_labels_dir: Path,
    output_dir: Path,
    categories: Sequence[str],
    names: Mapping[str, str],
    code_version: str | None = None,
) -> dict[str, Any]:
    """Write ``figuras.tex`` into ``output_dir`` (through a ``.partial`` file renamed at the end).

    Reads only analysis outputs: ``audit.json``; ``rq2_structure`` of ``gold_dir`` (ours) and
    ``compare_dir`` (the imported Agent READMEs files); ``artifact_labels`` of our RQ1
    classification (``labels_dir``) and ``section_labels`` of the Agent READMEs classification
    (``compare_labels_dir``). The size filter is the ``min_artifact_bytes`` of the analysis.
    """
    manifest = json.loads(_require(gold_dir / ANALYSIS_MANIFEST).read_text(encoding="utf-8"))
    min_bytes = manifest["analysis_config"]["min_artifact_bytes"]
    audit = json.loads(_require(audit_dir / AUDIT_JSON).read_text(encoding="utf-8"))
    structure, compared = _structure(gold_dir), _structure(compare_dir)
    ours_labels = pl.read_parquet(_require(labels_dir / ARTIFACT_LABELS), columns=_LABEL_COLUMNS)
    ar_labels = pl.read_parquet(
        _require(compare_labels_dir / SECTION_LABELS), columns=_LABEL_COLUMNS
    )

    pipeline = pipeline_numbers(audit, structure, min_bytes)
    elements = (element_counts(structure, min_bytes), element_counts(compared, min_bytes))
    prevalence = prevalence_columns(ours_labels, structure, ar_labels, min_bytes, compared)
    sources = {
        "Analysis": f"{gold_dir} (code {manifest.get('code_version')})",
        "Audit": f"{audit_dir} (code {audit.get('code_version')})",
        "Agent READMEs": str(compare_dir),
        "RQ1 labels": str(labels_dir),
        "RQ1 labels (Agent READMEs)": str(compare_labels_dir),
        "Code version": code_version,
    }
    tex = figures_tex(pipeline, elements, min_bytes, prevalence, categories, names, sources)
    output_dir.mkdir(parents=True, exist_ok=True)
    partial = output_dir / f"{FIGURES_TEX}.partial"
    partial.write_text(tex, encoding="utf-8")
    partial.replace(output_dir / FIGURES_TEX)
    order = category_order(prevalence, categories)
    return {
        "output": str(output_dir / FIGURES_TEX),
        "pipeline": {
            "discovered": pipeline.discovered,
            "evaluated": pipeline.evaluated,
            "selected": pipeline.selected,
            "mined": pipeline.mined,
            "sample_files": pipeline.sample_files,
            "sample_repositories": pipeline.sample_repositories,
        },
        "elements": {
            name: {
                file_type: {"files": counts.files}
                | {element: counts.percent(element) for element in FIGURE_ELEMENTS}
                for file_type, counts in data.items()
                if file_type in INSTRUCTION_TYPES
            }
            for name, data in zip(("ours", "agentreadmes"), elements, strict=True)
        },
        "prevalence": {
            f"{file_type} {cut}": {"files": counts.files}
            | {category: counts.percent(category) for category in order}
            for (file_type, cut), counts in prevalence.items()
        },
    }
