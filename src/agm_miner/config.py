"""Runtime configuration.

Two layers are kept apart on purpose:

* :class:`Settings` holds infrastructure and secrets (tokens, database URL, directories). It is read
  from the environment / ``.env`` and is never part of the research record.
* :class:`StudyConfig` holds every methodological decision (search queries, selection criteria,
  mining limits, analysis thresholds). It is loaded from a versioned YAML file and a snapshot of it
  is stored with every pipeline run, so any dataset can be traced back to the exact criteria used.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    github_token: SecretStr | None = Field(default=None, alias="GITHUB_TOKEN")
    database_url: str = Field(alias="DATABASE_URL")

    data_dir: Path = Field(default=Path("data"), alias="DATA_DIR")
    clone_dir: Path = Field(default=Path("data/clones"), alias="CLONE_DIR")
    study_config_path: Path = Field(default=Path("study.yaml"), alias="STUDY_CONFIG_PATH")

    search_rate_limit_min_remaining: int = Field(default=2, alias="SEARCH_RATE_LIMIT_MIN_REMAINING")
    graphql_rate_limit_min_remaining: int = Field(
        default=100, alias="GRAPHQL_RATE_LIMIT_MIN_REMAINING"
    )
    rate_limit_sleep_buffer_seconds: int = Field(default=5, alias="RATE_LIMIT_SLEEP_BUFFER_SECONDS")
    github_max_retries: int = Field(default=5, alias="GITHUB_MAX_RETRIES")

    git_timeout_seconds: int = Field(default=1800, alias="GIT_TIMEOUT_SECONDS")

    @property
    def bronze_dir(self) -> Path:
        return self.data_dir / "bronze"

    @property
    def gold_dir(self) -> Path:
        return self.data_dir / "gold"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DiscoveryConfig(_StrictModel):
    queries: list[str] = Field(
        default_factory=lambda: [
            "filename:CLAUDE.md",
            "filename:AGENTS.md",
            "path:.claude/skills filename:SKILL.md",
        ]
    )
    # GitHub code search only indexes files smaller than 384 KB.
    size_min: int = 0
    size_max: int = 384_000
    # The Search API never returns more than this many results for a single query.
    max_results_per_query: int = 1000
    page_size: int = 100


class SelectionConfig(_StrictModel):
    min_stars: int = 50
    min_commits: int = 10
    exclude_forks: bool = True
    exclude_archived: bool = True
    exclude_templates: bool = True
    exclude_mirrors: bool = True
    # A repository is considered active when it had a push within this many days before
    # ``reference_date``. ``None`` disables the criterion.
    active_within_days: int | None = 365
    # Fixed so that re-running selection later yields the same result. Defaults to the run date
    # when omitted (the effective value is stored in the run record).
    reference_date: date | None = None
    # Case-insensitive whole-word matches (singular and plural) against name, description and
    # topics flag tutorials, curated lists and similar non-product repositories.
    exclude_keywords: list[str] = Field(
        default_factory=lambda: [
            "awesome",
            "tutorial",
            "course",
            "boilerplate",
            "cheatsheet",
            "example",
            "starter",
            "template",
            "dotfiles",
            "prompt",
        ]
    )
    graphql_batch_size: int = 25


class MiningConfig(_StrictModel):
    # Files matching these globs are ignored even if they look like artifacts (vendored code).
    exclude_path_globs: list[str] = Field(
        default_factory=lambda: [
            "node_modules/**",
            "**/node_modules/**",
            "vendor/**",
            "**/vendor/**",
            "third_party/**",
            "**/third_party/**",
        ]
    )
    # Contents above this size are not stored (size and blob id still are).
    max_content_bytes: int = 2 * 1024 * 1024
    # Rename/copy detection needs the blobs of every added/deleted file in a commit. Commits
    # touching more files than this only get exact (same blob) rename detection.
    rename_candidate_limit: int = 200
    # Number of blob ids sent in a single partial-clone prefetch request.
    prefetch_batch_size: int = 500


class AnalysisConfig(_StrictModel):
    # Two consecutive changes closer than this are part of the same burst (RQ3).
    burst_window_hours: int = 24
    # Minimum prose characters needed to trust language detection / readability.
    min_chars_for_language: int = 200
    # Author role thresholds, expressed as share of the author's file changes.
    docs_focused_min_share: float = 0.8
    code_focused_min_share: float = 0.5
    # Regular expressions (case-insensitive) matched against "Name <email>" of commit authors
    # and co-authors. Agent patterns are deliberately specific (tool e-mails and bot logins):
    # a bare "claude" would also match human developers named Claude.
    bot_patterns: list[str] = Field(
        default_factory=lambda: [
            r"\[bot\]",
            r"^dependabot",
            r"^renovate",
            r"^github-actions",
            r"^pre-commit-ci",
            r"^snyk-bot",
        ]
    )
    agent_patterns: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "claude": [r"<noreply@anthropic\.com>", r"\bclaude(-code)?\[bot\]"],
            "codex": [
                r"\bcodex\[bot\]",
                r"chatgpt-codex-connector",
                r"<(codex|noreply)@openai\.com>",
            ],
            "copilot": [r"\bcopilot(-swe-agent)?\[bot\]", r"^copilot <"],
            "cursor": [r"\bcursor(agent)?\[bot\]", r"<cursoragent@cursor\.com>"],
            "gemini": [r"gemini-code-assist\[bot\]", r"google-labs-jules\[bot\]"],
            "devin": [r"devin-ai-integration\[bot\]"],
            "aider": [r"\(aider\) <"],
        }
    )


class StudyConfig(_StrictModel):
    version: int = 1
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    mining: MiningConfig = Field(default_factory=MiningConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)

    def snapshot(self) -> dict:
        return self.model_dump(mode="json")

    def fingerprint(self) -> str:
        canonical = json.dumps(self.snapshot(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_study_config(path: Path | str | None = None) -> StudyConfig:
    """Load the study configuration; a missing file means "all defaults"."""
    resolved = Path(path) if path is not None else get_settings().study_config_path
    if not resolved.exists():
        return StudyConfig()
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    return StudyConfig.model_validate(raw)
