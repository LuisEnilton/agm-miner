"""Structural parsing of Markdown artifacts (RQ1 sections, RQ2 structure).

Parsing uses markdown-it-py (CommonMark + GFM tables/strikethrough), never regular expressions
over raw text, so fenced code, HTML blocks and inline code are recognised correctly.

* YAML frontmatter (``---`` … ``---`` at the top, used by ``SKILL.md``) is split off and parsed.
* The document is cut into *sections*: the text between a heading and the next heading of any
  level. Each section carries its heading path (ancestor headings), so H2/H3 titles can be used
  directly as input for content categorisation (Chatlatanagulchai et al.).
* *Prose* is the text of paragraphs, headings, list items, tables and blockquotes, excluding code
  and HTML; word counts and language detection use prose only.
* *Readability text* is the prose of paragraphs and list items only (no headings or table cells),
  one block per line, each block ending with ``.``, ``!`` or ``?`` (a final ``:``, ``;`` or ``,``
  becomes a full stop). textstat only splits sentences on those three marks, so without this a
  document made of bullet points would count as a single sentence. Blocks of one or two words
  (labels, bare links) are left out: textstat does not count them as sentences but would still
  count their words, inflating the words per sentence.
* A UTF-8 byte order mark is ignored, bare URLs count as links (linkify) and task items
  (``- [ ] ...``) are only counted in list items.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)?^(?:---|\.\.\.)[ \t]*$\r?\n?", re.S | re.M
)
_WORD_RE = re.compile(r"\w+(?:['’-]\w+)*")
_TASK_RE = re.compile(r"^\[[ xX]\]\s")
_SENTENCE_END = (".", "!", "?")
_SOFT_END = ":;,"
_MIN_SENTENCE_WORDS = 3  # textstat ignores shorter sentences when counting them
_CLOSERS = ")]}\"'’”»*_"
_BOM = "\ufeff"

_PARSER = MarkdownIt("commonmark", {"html": True, "linkify": True}).enable(
    ["table", "strikethrough", "linkify"]
)


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str
    line: int  # 0-based first line in the original document
    end_line: int  # 0-based line after the heading (setext headings span two lines)


@dataclass(slots=True)
class ElementCounts:
    paragraphs: int = 0
    code_blocks: int = 0
    inline_code: int = 0
    bullet_lists: int = 0
    ordered_lists: int = 0
    list_items: int = 0
    task_items: int = 0
    tables: int = 0
    links: int = 0
    images: int = 0
    blockquotes: int = 0
    html_blocks: int = 0
    horizontal_rules: int = 0
    emphasis: int = 0
    code_languages: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedDocument:
    frontmatter: dict[str, Any] | None
    frontmatter_error: str | None
    has_frontmatter: bool
    headings: list[Heading]
    sections: list[SectionRecord]
    counts: ElementCounts
    prose: str
    readability_text: str
    line_count: int

    @property
    def word_count(self) -> int:
        return count_words(self.prose)

    def heading_tree(self) -> list[dict[str, Any]]:
        """Nested headings: ``[{"level": 1, "title": "...", "children": [...]}, ...]``."""
        root: list[dict[str, Any]] = []
        stack: list[tuple[int, list[dict[str, Any]]]] = [(0, root)]
        for heading in self.headings:
            while stack[-1][0] >= heading.level:
                stack.pop()
            node = {"level": heading.level, "title": heading.text, "children": []}
            stack[-1][1].append(node)
            stack.append((heading.level, node["children"]))
        return root


@dataclass(frozen=True, slots=True)
class SectionRecord:
    position: int
    level: int
    heading: str | None
    path: tuple[str, ...]
    path_levels: tuple[int, ...]
    text: str

    def ancestor(self, level: int) -> str | None:
        for title, title_level in zip(self.path, self.path_levels, strict=True):
            if title_level == level:
                return title
        return None


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text))


def split_frontmatter(text: str) -> tuple[str | None, str, int]:
    """Return (frontmatter source or None, body, number of lines removed)."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None, text, 0
    removed = text[: match.end()]
    return match.group("body"), text[match.end() :], removed.count("\n")


def parse_document(text: str) -> ParsedDocument:
    text = text.removeprefix(_BOM)
    frontmatter_source, body, offset = split_frontmatter(text)
    frontmatter: dict[str, Any] | None = None
    frontmatter_error: str | None = None
    if frontmatter_source is not None:
        try:
            loaded = yaml.safe_load(frontmatter_source)
        except yaml.YAMLError as exc:
            frontmatter_error = f"invalid YAML: {exc.__class__.__name__}"
        else:
            if loaded is None:
                frontmatter = {}
            elif isinstance(loaded, dict):
                frontmatter = {str(key): value for key, value in loaded.items()}
            else:
                frontmatter_error = f"frontmatter is a {type(loaded).__name__}, not a mapping"

    tokens = _PARSER.parse(body)
    counts = ElementCounts()
    headings: list[Heading] = []
    prose_parts: list[str] = []
    readable_parts: list[str] = []

    for index, token in enumerate(tokens):
        kind = token.type
        if kind == "heading_open":
            inline = tokens[index + 1]
            title = _inline_text(inline)
            first_line, after_line = token.map or (0, 1)
            headings.append(
                Heading(int(token.tag[1]), title, first_line + offset, after_line + offset)
            )
        elif kind == "paragraph_open":
            # Items of a tight list wrap their text in hidden paragraphs: they are list items.
            if not token.hidden:
                counts.paragraphs += 1
        elif kind in ("fence", "code_block"):
            counts.code_blocks += 1
            language = token.info.strip().split(" ")[0].lower() if token.info else ""
            if language:
                counts.code_languages.append(language)
        elif kind == "bullet_list_open":
            counts.bullet_lists += 1
        elif kind == "ordered_list_open":
            counts.ordered_lists += 1
        elif kind == "list_item_open":
            counts.list_items += 1
        elif kind == "table_open":
            counts.tables += 1
        elif kind == "blockquote_open":
            counts.blockquotes += 1
        elif kind == "html_block":
            counts.html_blocks += 1
        elif kind == "hr":
            counts.horizontal_rules += 1
        elif kind == "inline":
            text_content = _inline_text(token, counts)
            in_paragraph = index > 0 and tokens[index - 1].type == "paragraph_open"
            in_list_item = in_paragraph and index > 1 and tokens[index - 2].type == "list_item_open"
            if in_list_item and _TASK_RE.match(token.content):
                counts.task_items += 1
            if text_content:
                prose_parts.append(text_content)
                if in_paragraph and count_words(text_content) >= _MIN_SENTENCE_WORDS:
                    readable_parts.append(_as_sentence(text_content))

    lines = body.split("\n")
    sections = _sections(headings, lines, offset)
    return ParsedDocument(
        frontmatter=frontmatter,
        frontmatter_error=frontmatter_error,
        has_frontmatter=frontmatter_source is not None,
        headings=headings,
        sections=sections,
        counts=counts,
        prose="\n".join(prose_parts),
        readability_text="\n".join(readable_parts),
        line_count=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
    )


def _as_sentence(text: str) -> str:
    """End a block with a full stop unless it already ends a sentence (also before a closing
    quote or bracket, as in ``(see below.)``); a final ``:``, ``;`` or ``,`` is replaced."""
    if text.rstrip(_CLOSERS).endswith(_SENTENCE_END):
        return text
    return f"{text.rstrip(_SOFT_END)}."


def _inline_text(token: Token, counts: ElementCounts | None = None) -> str:
    """Visible text of an inline token (code spans excluded); optionally count inline elements."""
    parts: list[str] = []
    for child in token.children or []:
        kind = child.type
        if kind in ("text", "softbreak", "hardbreak"):
            parts.append(child.content if kind == "text" else " ")
        elif kind == "code_inline":
            if counts is not None:
                counts.inline_code += 1
        elif kind == "image":
            if counts is not None:
                counts.images += 1
            parts.append(child.content)  # alt text
        elif counts is not None and kind == "link_open":
            counts.links += 1
        elif counts is not None and kind in ("em_open", "strong_open", "s_open"):
            counts.emphasis += 1
    return "".join(parts).strip()


def _sections(headings: list[Heading], body_lines: list[str], offset: int) -> list[SectionRecord]:
    sections: list[SectionRecord] = []
    boundaries = [heading.line - offset for heading in headings]
    first = boundaries[0] if boundaries else len(body_lines)
    preamble = "\n".join(body_lines[:first]).strip()
    if preamble:
        sections.append(SectionRecord(0, 0, None, (), (), preamble))

    stack: list[Heading] = []
    for index, heading in enumerate(headings):
        while stack and stack[-1].level >= heading.level:
            stack.pop()
        stack.append(heading)
        start = heading.end_line - offset
        end = boundaries[index + 1] if index + 1 < len(boundaries) else len(body_lines)
        sections.append(
            SectionRecord(
                position=len(sections),
                level=heading.level,
                heading=heading.text,
                path=tuple(item.text for item in stack),
                path_levels=tuple(item.level for item in stack),
                text="\n".join(body_lines[start:end]).strip(),
            )
        )
    return sections
