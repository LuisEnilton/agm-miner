from __future__ import annotations

import json

import pytest
import textstat

from agm_miner.analysis.markdown import count_words, parse_document, split_frontmatter
from agm_miner.analysis.metrics import compute_metrics
from agm_miner.analysis.structure_diff import diff_documents

CLAUDE_MD = """Intro line before any heading.

# Project

Overview paragraph with a [link](https://example.org) and `inline code`.

## Build

Run the following:

```bash
make build   # words inside code are not prose
```

- [ ] pending task
- [x] done task

## Style
### Python

Use **ruff** and _type hints_.

| Tool | Use |
|------|-----|
| ruff | lint |

Setext Heading
--------------

> Quote here.

![diagram](img.png)
"""

SKILL_MD = """---
name: pdf
description: Fill PDF forms
allowed-tools: [Read, Write]
---
# PDF

Use the scripts.
"""


def test_headings_sections_and_paths() -> None:
    document = parse_document(CLAUDE_MD)
    assert [(h.level, h.text) for h in document.headings] == [
        (1, "Project"),
        (2, "Build"),
        (2, "Style"),
        (3, "Python"),
        (2, "Setext Heading"),
    ]
    paths = [section.path for section in document.sections]
    assert paths == [
        (),
        ("Project",),
        ("Project", "Build"),
        ("Project", "Style"),
        ("Project", "Style", "Python"),
        ("Project", "Setext Heading"),
    ]
    python = document.sections[4]
    assert python.ancestor(2) == "Style" and python.ancestor(3) == "Python"
    assert python.ancestor(1) == "Project"
    assert document.sections[0].text == "Intro line before any heading."
    assert document.sections[2].text.startswith("Run the following:")
    # The setext underline belongs to the heading, not to the section body.
    assert document.sections[5].text.startswith("> Quote here.")
    assert document.heading_tree()[0]["children"][1]["children"][0]["title"] == "Python"


def test_element_counts_and_prose() -> None:
    document = parse_document(CLAUDE_MD)
    counts = document.counts
    assert counts.code_blocks == 1 and counts.code_languages == ["bash"]
    assert counts.inline_code == 1
    assert counts.links == 1 and counts.images == 1
    assert counts.bullet_lists == 1 and counts.list_items == 2 and counts.task_items == 2
    assert counts.tables == 1 and counts.blockquotes == 1
    assert counts.emphasis == 2
    assert "make build" not in document.prose
    assert "inline code" not in document.prose
    assert "Overview paragraph" in document.prose


def test_tight_list_items_are_not_paragraphs() -> None:
    tight = parse_document("# T\n\n- a\n- b\n- c\n\nA paragraph.\n").counts
    assert tight.paragraphs == 1 and tight.list_items == 3
    # Items of a loose list (separated by blank lines) hold real paragraphs.
    loose = parse_document("- a\n\n- b\n").counts
    assert loose.paragraphs == 2 and loose.list_items == 2


def test_task_items_only_in_lists() -> None:
    assert parse_document("[ ] not a task, just a paragraph\n").counts.task_items == 0
    assert parse_document("1. [x] done\n2. [ ] todo\n3. plain\n").counts.task_items == 2


def test_bare_urls_are_links() -> None:
    counts = parse_document("See https://example.com and [docs](docs/a.md).\n").counts
    assert counts.links == 2


def test_readability_text_treats_each_block_as_a_sentence() -> None:
    document = parse_document(
        "# Commands\n\n"
        "Intro paragraph that already ends.\n\n"
        "- Run the tests\n"
        "- Ask first (see below.)\n\n"
        "| Head cell | Other |\n|---|---|\n| body cell | x |\n"
    )
    assert document.readability_text.split("\n") == [
        "Intro paragraph that already ends.",
        "Run the tests.",
        "Ask first (see below.)",
    ]
    # Headings and table cells are still prose (words, language), just not sentences.
    assert "Commands" in document.prose and "body cell" in document.prose


def test_readability_text_matches_textstat_sentences() -> None:
    document = parse_document(
        "Run the following commands:\n\n"
        "- install all the dependencies;\n"
        "- [Concepts](docs/concepts.md)\n"
        "- Architecture\n"
        "- run the whole test suite\n"
    )
    # textstat only splits on .!? and ignores sentences of one or two words.
    assert document.readability_text.split("\n") == [
        "Run the following commands.",
        "install all the dependencies.",
        "run the whole test suite.",
    ]
    assert textstat.sentence_count(document.readability_text) == 3


def test_readability_of_bullet_only_document() -> None:
    bullets = [
        "Run the unit tests with the test command",
        "Build the package before publishing it",
        "Keep the public API stable across minor releases",
        "Prefer small focused pull requests",
        "Use strict typing in every module",
        "Avoid default exports in modules",
        "Document every public function",
    ]
    text = "# Package guide\n\n## Rules\n\n" + "".join(f"- {item}\n" for item in bullets)
    metrics, _ = compute_metrics(text)
    assert metrics.language == "en"
    # Joined without punctuation, these bullets used to be a single sentence (grade > 20).
    assert metrics.flesch_kincaid_grade is not None and metrics.flesch_kincaid_grade < 12
    assert metrics.flesch_reading_ease is not None and metrics.flesch_reading_ease > 40


def test_document_without_sentences_has_no_readability() -> None:
    headings = "".join(f"## Section about the build number {i}\n" for i in range(12))
    metrics, document = compute_metrics(headings)
    assert document.readability_text == ""
    assert metrics.flesch_reading_ease is None and metrics.flesch_kincaid_grade is None


def test_frontmatter_parsing() -> None:
    document = parse_document(SKILL_MD)
    assert document.has_frontmatter
    assert document.frontmatter == {
        "name": "pdf",
        "description": "Fill PDF forms",
        "allowed-tools": ["Read", "Write"],
    }
    assert [h.text for h in document.headings] == ["PDF"]
    # Heading lines refer to the original document (frontmatter included).
    assert document.headings[0].line == 5


@pytest.mark.parametrize(
    ("text", "has", "error"),
    [
        ("---\n: bad: [yaml\n---\n# X\n", True, "invalid YAML"),
        ("---\n- a\n- b\n---\n# X\n", True, "not a mapping"),
        ("---\n---\n# X\n", True, None),
        ("# No frontmatter\n---\n", False, None),
    ],
)
def test_frontmatter_edge_cases(text: str, has: bool, error: str | None) -> None:
    document = parse_document(text)
    assert document.has_frontmatter is has
    if error is None:
        assert document.frontmatter_error is None
    else:
        assert error in document.frontmatter_error


def test_frontmatter_after_byte_order_mark() -> None:
    text = "\ufeff---\nname: x\n---\n# H\n"
    document = parse_document(text)
    assert document.has_frontmatter and document.frontmatter == {"name": "x"}
    assert document.headings[0].line == 3
    metrics, _ = compute_metrics(text)
    assert metrics.chars == len(text)


def test_split_frontmatter_offsets() -> None:
    source, body, removed = split_frontmatter("---\na: 1\n---\nbody\n")
    assert source == "a: 1" and body == "body\n" and removed == 3


def test_count_words() -> None:
    assert count_words("Don't use state-of-the-art tools, ok? 3 files") == 7


def test_metrics_for_english_document() -> None:
    text = "# Guide\n\n" + "This project uses a simple build. Run the tests before you push. " * 10
    metrics, _ = compute_metrics(text)
    assert metrics.language == "en"
    assert metrics.flesch_reading_ease is not None and metrics.flesch_kincaid_grade is not None
    assert metrics.h1 == 1 and metrics.max_heading_depth == 1
    assert json.loads(metrics.heading_tree_json)[0]["title"] == "Guide"


def test_readability_only_for_english() -> None:
    text = (
        "# Guia\n\n" + "Este projeto usa um processo simples de build. Rode os testes antes. " * 10
    )
    metrics, _ = compute_metrics(text)
    assert metrics.language == "pt"
    assert metrics.flesch_reading_ease is None


def test_short_documents_have_no_language() -> None:
    metrics, _ = compute_metrics("# Tiny\n\nshort")
    assert metrics.language is None and metrics.flesch_reading_ease is None


def test_skill_metrics_expose_frontmatter_keys() -> None:
    metrics, _ = compute_metrics(SKILL_MD)
    assert metrics.frontmatter_valid is True
    assert metrics.frontmatter_keys == ["allowed-tools", "description", "name"]


def test_structure_diff() -> None:
    before = parse_document("# A\n\n## Setup\nold\n\n## Style\nsame\n")
    after = parse_document("# A\n\n## Setup\nnew\n\n## Style\nsame\n\n## Security\nx\n")
    diff = diff_documents(before, after)
    assert diff.sections_added == ["A > Security"]
    assert diff.sections_removed == []
    assert diff.sections_modified == ["A > Setup"]
    assert diff.is_structural


def test_structure_diff_frontmatter_and_creation() -> None:
    before = parse_document("---\nname: a\ndescription: x\n---\n# S\n")
    after = parse_document("---\nname: a\ndescription: y\nallowed-tools: [Read]\n---\n# S\n")
    diff = diff_documents(before, after)
    assert diff.frontmatter_added == ["allowed-tools"]
    assert diff.frontmatter_modified == ["description"]
    assert diff.sections_modified == []
    created = diff_documents(None, after)
    assert created.sections_added == ["S"] and "name" in created.frontmatter_added


def test_repeated_headings_are_disambiguated() -> None:
    before = parse_document("## Notes\na\n\n## Notes\nb\n")
    after = parse_document("## Notes\na\n")
    assert diff_documents(before, after).sections_removed == ["Notes #2"]
