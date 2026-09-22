"""Structural and readability metrics of one artifact version (RQ2)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import textstat
from langdetect import DetectorFactory, LangDetectException, detect

from agm_miner.analysis.markdown import ParsedDocument, parse_document

# langdetect is probabilistic; a fixed seed makes results reproducible.
DetectorFactory.seed = 0


@dataclass(frozen=True, slots=True)
class DocumentMetrics:
    lines: int
    chars: int
    words: int
    headings: int
    h1: int
    h2: int
    h3: int
    h4_plus: int
    max_heading_depth: int
    sections: int
    paragraphs: int
    code_blocks: int
    code_languages: list[str]
    inline_code: int
    bullet_lists: int
    ordered_lists: int
    list_items: int
    task_items: int
    tables: int
    links: int
    images: int
    blockquotes: int
    html_blocks: int
    emphasis: int
    has_frontmatter: bool
    frontmatter_valid: bool | None
    frontmatter_keys: list[str]
    frontmatter_json: str | None
    heading_tree_json: str
    language: str | None
    flesch_reading_ease: float | None
    flesch_kincaid_grade: float | None

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


def detect_language(prose: str, min_chars: int) -> str | None:
    if len(prose) < min_chars:
        return None
    try:
        return detect(prose)
    except LangDetectException:
        return None


def compute_metrics(
    text: str, min_chars_for_language: int = 200
) -> tuple[DocumentMetrics, ParsedDocument]:
    document = parse_document(text)
    levels = [heading.level for heading in document.headings]
    language = detect_language(document.prose, min_chars_for_language)
    # Flesch formulas are only defined for English text.
    readable = language == "en"
    frontmatter_valid = None if not document.has_frontmatter else document.frontmatter is not None
    metrics = DocumentMetrics(
        lines=document.line_count,
        chars=len(text),
        words=document.word_count,
        headings=len(levels),
        h1=levels.count(1),
        h2=levels.count(2),
        h3=levels.count(3),
        h4_plus=sum(1 for level in levels if level >= 4),
        max_heading_depth=max(levels, default=0),
        sections=len(document.sections),
        paragraphs=document.counts.paragraphs,
        code_blocks=document.counts.code_blocks,
        code_languages=sorted(set(document.counts.code_languages)),
        inline_code=document.counts.inline_code,
        bullet_lists=document.counts.bullet_lists,
        ordered_lists=document.counts.ordered_lists,
        list_items=document.counts.list_items,
        task_items=document.counts.task_items,
        tables=document.counts.tables,
        links=document.counts.links,
        images=document.counts.images,
        blockquotes=document.counts.blockquotes,
        html_blocks=document.counts.html_blocks,
        emphasis=document.counts.emphasis,
        has_frontmatter=document.has_frontmatter,
        frontmatter_valid=frontmatter_valid,
        frontmatter_keys=sorted(document.frontmatter or {}),
        frontmatter_json=(
            json.dumps(document.frontmatter, sort_keys=True, default=str)
            if document.frontmatter is not None
            else None
        ),
        heading_tree_json=json.dumps(document.heading_tree(), ensure_ascii=False),
        language=language,
        flesch_reading_ease=textstat.flesch_reading_ease(document.prose) if readable else None,
        flesch_kincaid_grade=textstat.flesch_kincaid_grade(document.prose) if readable else None,
    )
    return metrics, document
