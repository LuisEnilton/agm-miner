from __future__ import annotations


def is_markdown_file(file_path: str) -> bool:
    normalized = file_path.lower()
    return normalized.endswith(".md") or normalized.endswith(".markdown")


def is_skill_file(file_path: str) -> bool:
    normalized = file_path.lower()
    if not normalized.startswith(".claude/skills/"):
        return False
    return normalized.endswith((".md", ".markdown", ".yaml", ".yml"))


def is_extractable_artifact(file_path: str) -> bool:
    normalized = file_path.lower()
    if normalized.endswith("claude.md") or normalized.endswith("agents.md"):
        return True
    return is_skill_file(file_path)


def count_words(text: str | None) -> int:
    if not text:
        return 0
    return len(text.split())


def should_skip_raw_content(content: str | None) -> bool:
    if not content:
        return True
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        return True
    return False
