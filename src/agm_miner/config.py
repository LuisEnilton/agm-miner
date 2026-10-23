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
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    github_token: SecretStr | None = Field(default=None, alias="GITHUB_TOKEN")
    # LLM credentials for the RQ1 content coding (agm rq1 ...); never printed.
    anthropic_api_key: SecretStr | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openrouter_api_key: SecretStr | None = Field(default=None, alias="OPENROUTER_API_KEY")
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

    # How commands show their progress: "live" panel, "plain" periodic log lines, "off", or
    # "auto" (live in an interactive terminal, plain otherwise).
    progress_mode: Literal["auto", "live", "plain", "off"] = Field(
        default="auto", alias="AGM_PROGRESS"
    )

    @property
    def bronze_dir(self) -> Path:
        return self.data_dir / "bronze"

    @property
    def gold_dir(self) -> Path:
        return self.data_dir / "gold"

    @property
    def rq1_dir(self) -> Path:
        return self.data_dir / "rq1"


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
    # Only repositories created before this date: the artifacts are then additions to existing
    # projects rather than projects built around the agent tools. ``None`` disables the criterion.
    created_before: date | None = date(2024, 1, 1)
    # SPDX ids of the accepted licenses (case-insensitive); ``None`` accepts any repository,
    # even unlicensed ones. The default is the OSI-approved licenses GitHub detects.
    allowed_licenses: list[str] | None = Field(
        default_factory=lambda: [
            "0BSD",
            "AFL-3.0",
            "AGPL-3.0",
            "Apache-2.0",
            "Artistic-2.0",
            "BSD-2-Clause",
            "BSD-2-Clause-Patent",
            "BSD-3-Clause",
            "BSL-1.0",
            "CECILL-2.1",
            "ECL-2.0",
            "EPL-1.0",
            "EPL-2.0",
            "EUPL-1.1",
            "EUPL-1.2",
            "GPL-2.0",
            "GPL-3.0",
            "ISC",
            "LGPL-2.1",
            "LGPL-3.0",
            "LPPL-1.3c",
            "MIT",
            "MIT-0",
            "MPL-2.0",
            "MS-PL",
            "MS-RL",
            "MulanPSL-2.0",
            "NCSA",
            "OFL-1.1",
            "OSL-3.0",
            "PostgreSQL",
            "UPL-1.0",
            "Unlicense",
            "Zlib",
        ]
    )
    # A software repository has a programming language as its primary language (GitHub
    # Linguist); these markup/prose languages do not count. Disabled with ``False``.
    require_code_language: bool = True
    non_code_languages: list[str] = Field(
        default_factory=lambda: [
            "Markdown",
            "MDX",
            "HTML",
            "reStructuredText",
            "AsciiDoc",
            "Text",
        ]
    )
    # A repository is considered active when the latest commit of its default branch is at most
    # this many days older than ``reference_date``. ``None`` disables the criterion.
    active_within_days: int | None = 365
    # Fixed so that re-running selection later yields the same result. Defaults to the run date
    # when omitted (the effective value is stored in the run record).
    reference_date: date | None = None
    # Case-insensitive whole-word matches (singular and plural) against name, description and
    # topics flag tutorials, curated lists and similar non-product repositories. Ambiguous words
    # ("example", "template", "prompt") are left out: they reject real software ("a template
    # engine", "prompt toolkit"); GitHub template repositories are covered by exclude_templates.
    exclude_keywords: list[str] = Field(
        default_factory=lambda: [
            "awesome",
            "tutorial",
            "course",
            "boilerplate",
            "cheatsheet",
            "starter",
            "dotfiles",
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
    # Study sample (applied to the mined data, see analysis/datasets.py):
    # an instruction file or skill manifest belongs to the sample when its latest version has
    # at least this many bytes (symlinks never do: their target is an artifact of its own) ...
    min_artifact_bytes: int = 2048
    # ... and a repository when it has one such artifact and this many human authors (neither
    # bot_patterns nor agent_patterns) in its whole history.
    min_human_contributors: int = 2
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


class Rq1Config(_StrictModel):
    """RQ1 content coding: label creation with an LLM on a sample, then classification of every
    section against the curated codebook (see src/agm_miner/rq1/)."""

    # Label-creation sample: stratified by tool x artifact kind x length, at most
    # ``per_repo_cap`` sections per repository so large repositories do not dominate.
    sample_size: int = Field(default=400, gt=0)
    per_repo_cap: int = Field(default=5, gt=0)
    # Section lengths (words) separating the short / medium / long strata (increasing).
    length_bins: list[int] = Field(default_factory=lambda: [50, 150])
    seed: int = 20261022
    # Where the models run: "anthropic" (Claude through the official SDK, Batches API; the final
    # collection) or "openrouter" (any OpenRouter model, e.g. free ones to test the pipeline).
    provider: Literal["anthropic", "openrouter"] = "anthropic"
    # OpenRouter free models allow 20 requests per minute.
    openrouter_requests_per_minute: int = Field(default=20, gt=0)
    # Models and effort per phase, as the provider names them. Effort applies to Claude only.
    # (The Claude API no longer accepts sampling parameters, so stability is measured, not
    # fixed: see agm rq1 validate.)
    propose_model: str = "claude-opus-5-5"
    propose_effort: str = "high"
    consolidate_model: str = "claude-opus-5-5"
    consolidate_effort: str = "high"
    classify_model: str = "claude-sonnet-5-5"
    classify_effort: str = "low"
    # Sections per classification request (a file's sections are sent together, in chunks).
    chunk_sections: int = Field(default=30, gt=0)
    # Blind validation sample labelled by hand.
    validation_size: int = Field(default=300, gt=0)

    @field_validator("length_bins")
    @classmethod
    def _increasing_bins(cls, bins: list[int]) -> list[int]:
        if any(limit <= 0 for limit in bins) or bins != sorted(set(bins)):
            raise ValueError("length_bins must be positive and strictly increasing")
        return bins

    def model_for(self, phase: str) -> tuple[str, str]:
        """(model, effort) of a phase: propose, consolidate or classify."""
        return getattr(self, f"{phase}_model"), getattr(self, f"{phase}_effort")


class StudyConfig(_StrictModel):
    version: int = 1
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    selection: SelectionConfig = Field(default_factory=SelectionConfig)
    mining: MiningConfig = Field(default_factory=MiningConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    rq1: Rq1Config = Field(default_factory=Rq1Config)

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
