"""Coarse categorisation of repository files.

Used to profile what each contributor works on (RQ3: relation between the people who edit agent
artifacts and the kinds of files they touch) and to describe files co-changed with an artifact
(update triggers). Rules are evaluated in order; the first match wins.
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import PurePosixPath

from agm_miner.artifacts import classify_included


class FileCategory(StrEnum):
    AGENT_ARTIFACT = "agent_artifact"
    AGENT_CONFIG = "agent_config"
    TEST = "test"
    CI = "ci"
    DOCS = "docs"
    BUILD_CONFIG = "build_config"
    CODE = "code"
    ASSET = "asset"
    OTHER = "other"


_AGENT_CONFIG_PREFIXES = (".claude/", ".cursor/", ".codex/", ".gemini/", ".github/copilot")
_AGENT_CONFIG_NAMES = frozenset(
    {
        ".cursorrules",
        "gemini.md",
        ".windsurfrules",
        ".clinerules",
        ".mcp.json",
        "copilot-instructions.md",
    }
)

_TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing", "e2e"})
_TEST_FILE_RE = re.compile(
    r"(^test_.*\.py$)|(_test\.(py|go|rb|rs|exs?)$)|(\.(test|spec)\.[a-z0-9]+$)|(Tests?\.(java|kt|cs|swift)$)|(_spec\.rb$)",
)

_CI_PREFIXES = (".github/workflows/", ".circleci/", ".buildkite/", ".gitlab/")
_CI_NAMES = frozenset(
    {
        ".gitlab-ci.yml",
        ".travis.yml",
        "jenkinsfile",
        "azure-pipelines.yml",
        "appveyor.yml",
        "bitbucket-pipelines.yml",
        ".drone.yml",
    }
)

_DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".adoc", ".txt", ".mdx", ".org"})
_DOC_DIR_NAMES = frozenset({"docs", "doc", "documentation"})
_DOC_NAMES = frozenset({"license", "licence", "notice", "authors", "contributors", "changelog"})

_BUILD_NAMES = frozenset(
    {
        "makefile",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "poetry.lock",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "pipfile",
        "pipfile.lock",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "cmakelists.txt",
        "tsconfig.json",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        ".dockerignore",
        ".env.sample",
        ".env.example",
        "composer.json",
        "composer.lock",
        "mix.exs",
        "deno.json",
        "bun.lockb",
        "uv.lock",
    }
)
_BUILD_EXTENSIONS = frozenset(
    {
        ".toml",
        ".yaml",
        ".yml",
        ".json",
        ".ini",
        ".cfg",
        ".conf",
        ".lock",
        ".gradle",
        ".xml",
        ".properties",
        ".env",
        ".nix",
        ".tf",
        ".hcl",
    }
)

_CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ipynb",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".kts",
        ".scala",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".cs",
        ".fs",
        ".swift",
        ".m",
        ".mm",
        ".dart",
        ".lua",
        ".r",
        ".jl",
        ".ex",
        ".exs",
        ".erl",
        ".hs",
        ".ml",
        ".clj",
        ".elm",
        ".vue",
        ".svelte",
        ".astro",
        ".sh",
        ".bash",
        ".zsh",
        ".fish",
        ".ps1",
        ".sql",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".sass",
        ".less",
        ".zig",
        ".nim",
        ".v",
        ".sol",
        ".proto",
        ".graphql",
        ".gql",
        ".pl",
        ".pm",
        ".groovy",
        ".tf",
        ".wasm",
    }
)
_ASSET_EXTENSIONS = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".ico",
        ".webp",
        ".bmp",
        ".pdf",
        ".mp4",
        ".mov",
        ".mp3",
        ".wav",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
        ".eot",
        ".zip",
        ".gz",
        ".tar",
        ".jar",
        ".bin",
        ".dll",
        ".so",
        ".dylib",
        ".exe",
        ".csv",
        ".parquet",
        ".pkl",
        ".npy",
    }
)


def categorize(path: str, exclude_globs: list[str] | tuple[str, ...] = ()) -> FileCategory:
    """Category of a repository path; ``exclude_globs`` are the artifact exclusions of the study
    (a vendored ``node_modules/x/CLAUDE.md`` is not an artifact of the repository)."""
    if classify_included(path, exclude_globs) is not None:
        return FileCategory.AGENT_ARTIFACT

    posix = PurePosixPath(path)
    name = posix.name.lower()
    suffix = posix.suffix.lower()
    lowered = path.lower()
    directories = {part.lower() for part in posix.parts[:-1]}

    if lowered.startswith(_AGENT_CONFIG_PREFIXES) or name in _AGENT_CONFIG_NAMES:
        return FileCategory.AGENT_CONFIG
    if directories & _TEST_DIR_NAMES or _TEST_FILE_RE.search(posix.name):
        return FileCategory.TEST
    if lowered.startswith(_CI_PREFIXES) or name in _CI_NAMES:
        return FileCategory.CI
    if name in _BUILD_NAMES:
        return FileCategory.BUILD_CONFIG
    in_docs_dir = bool(directories & _DOC_DIR_NAMES) and suffix not in _CODE_EXTENSIONS
    if suffix in _DOC_EXTENSIONS or name in _DOC_NAMES or in_docs_dir:
        return FileCategory.DOCS
    if suffix in _CODE_EXTENSIONS:
        return FileCategory.CODE
    if suffix in _BUILD_EXTENSIONS or name.startswith("dockerfile"):
        return FileCategory.BUILD_CONFIG
    if suffix in _ASSET_EXTENSIONS:
        return FileCategory.ASSET
    return FileCategory.OTHER
