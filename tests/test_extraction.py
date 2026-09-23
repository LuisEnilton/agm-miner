from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from agm_miner.artifacts import ArtifactKind, ArtifactScope, ArtifactTool
from agm_miner.config import MiningConfig
from agm_miner.database.models import ChangeType, ContentOmission
from agm_miner.extraction import Extraction, decode_content, extract_repository, identity_key
from agm_miner.file_categories import FileCategory
from agm_miner.git.repository import GitRepository
from tests.gitrepo import RepoBuilder

pytestmark = pytest.mark.git

ANA = ("Ana Dev", "ana@example.org")
BOB = ("Bob", "bob@example.org")
BOB_OLD = ("bobby", "bob@old-laptop.local")
CAROL = ("Carol", "carol@example.org")

CLAUDE_V1 = "# Project\n\n## Setup\nRun `make`.\n"
CLAUDE_V2 = CLAUDE_V1 + "\n## Style\nUse ruff.\nKeep functions small.\n"
CLAUDE_V3 = CLAUDE_V2 + "\n## Security\nNever commit secrets.\n"
SKILL = "---\nname: pdf\ndescription: Fill PDF forms\n---\n# PDF skill\n"
PNG = b"\x89PNG\r\n\x1a\n\0\0\0binary"


def day(n: int) -> str:
    return f"2024-01-{n:02d}T12:00:00+00:00"


@pytest.fixture
def history(repo_builder: RepoBuilder) -> dict[str, str]:
    repo = repo_builder
    shas: dict[str, str] = {}
    shas["c1"] = repo.commit(
        "Initial commit\n\nCo-authored-by: Claude <noreply@anthropic.com>",
        {
            "README.md": "# Readme\n",
            "src/app.py": "print('hi')\n",
            "CLAUDE.md": CLAUDE_V1,
            ".mailmap": "Bob <bob@example.org> bobby <bob@old-laptop.local>\n",
        },
        author=ANA,
        date=day(1),
    )
    shas["c2"] = repo.commit(
        "Tweak app", {"src/app.py": "print('hello')\n"}, author=BOB_OLD, date=day(2)
    )
    shas["c3"] = repo.commit(
        "Document style and add pdf skill",
        {
            "CLAUDE.md": CLAUDE_V2,
            ".claude/skills/pdf/SKILL.md": SKILL,
            ".claude/skills/pdf/logo.png": PNG,
            "tests/test_app.py": "def test_app():\n    assert True\n",
        },
        author=ANA,
        date=day(3),
    )
    repo.checkout("feature", create=True)
    shas["c4"] = repo.commit("Security notes", {"CLAUDE.md": CLAUDE_V3}, author=BOB, date=day(4))
    repo.checkout("main")
    shas["c5"] = repo.commit("Docs", {"docs/guide.md": "guide\n"}, author=ANA, date=day(5))
    shas["merge"] = repo.merge("feature", author=ANA, date=day(6))
    repo.move("CLAUDE.md", "AGENTS.md")
    repo.symlink("CLAUDE.md", "AGENTS.md")
    shas["c7"] = repo.commit("Move instructions to AGENTS.md", author=ANA, date=day(7))
    shas["c8"] = repo.commit(
        "Nested instructions, drop pdf manifest",
        {
            "packages/api/CLAUDE.md": "# API\n\nUse FastAPI.\n",
            "node_modules/pkg/CLAUDE.md": "# vendored\n",
            ".claude/skills/pdf/SKILL.md": None,
        },
        author=CAROL,
        date=day(8),
    )
    repo.move("packages/api/CLAUDE.md", "packages/api/AGENTS.md")
    shas["c9"] = repo.commit("Rename nested file", author=CAROL, date=day(9))
    return shas


@pytest.fixture
def extraction(history: dict[str, str], repo_builder: RepoBuilder, tmp_path: Path) -> Extraction:
    clone = GitRepository.clone(repo_builder.url, tmp_path / "clone", timeout_seconds=60)
    return extract_repository(clone, MiningConfig())


def changes_of(extraction: Extraction, path: str) -> list[tuple[str, ChangeType]]:
    return [(c.sha, c.change_type) for c in extraction.changes if c.path == path]


def test_head_and_artifact_inventory(extraction: Extraction, history: dict[str, str]) -> None:
    assert extraction.head_sha == history["c9"]
    artifacts = extraction.artifacts
    assert set(artifacts) == {
        "CLAUDE.md",
        "AGENTS.md",
        ".claude/skills/pdf/SKILL.md",
        ".claude/skills/pdf/logo.png",
        "packages/api/CLAUDE.md",
        "packages/api/AGENTS.md",
    }
    assert artifacts["AGENTS.md"].match.tool is ArtifactTool.AGENTS
    assert artifacts["packages/api/AGENTS.md"].match.scope is ArtifactScope.NESTED
    assert artifacts[".claude/skills/pdf/SKILL.md"].match.kind is ArtifactKind.SKILL_MANIFEST
    assert artifacts[".claude/skills/pdf/SKILL.md"].exists_at_head is False
    assert artifacts["packages/api/CLAUDE.md"].exists_at_head is False
    assert artifacts["CLAUDE.md"].exists_at_head is True
    assert artifacts["CLAUDE.md"].head_blob_sha is not None


def test_full_history_including_branch_commits(
    extraction: Extraction, history: dict[str, str]
) -> None:
    assert changes_of(extraction, "CLAUDE.md") == [
        (history["c7"], ChangeType.TYPE_CHANGED),
        (history["c4"], ChangeType.MODIFIED),
        (history["c3"], ChangeType.MODIFIED),
        (history["c1"], ChangeType.ADDED),
    ]
    committed = {commit.sha for commit in extraction.commits}
    assert committed == {history[k] for k in ("c1", "c3", "c4", "c7", "c8", "c9")}
    assert history["merge"] not in committed


def test_rename_and_copy_lineage(extraction: Extraction, history: dict[str, str]) -> None:
    (agents_change,) = [c for c in extraction.changes if c.path == "AGENTS.md"]
    assert agents_change.change_type is ChangeType.COPIED
    assert agents_change.source_path == "CLAUDE.md"
    assert agents_change.similarity == 100
    assert agents_change.additions == 0 and agents_change.deletions == 0

    (nested,) = [c for c in extraction.changes if c.path == "packages/api/AGENTS.md"]
    assert nested.change_type is ChangeType.RENAMED
    assert nested.source_path == "packages/api/CLAUDE.md"
    assert changes_of(extraction, "packages/api/CLAUDE.md") == [
        (history["c9"], ChangeType.DELETED),
        (history["c8"], ChangeType.ADDED),
    ]


def test_numstat_and_blob_links(extraction: Extraction, history: dict[str, str]) -> None:
    by_sha = {c.sha: c for c in extraction.changes if c.path == "CLAUDE.md"}
    assert (by_sha[history["c1"]].additions, by_sha[history["c1"]].deletions) == (4, 0)
    assert (by_sha[history["c3"]].additions, by_sha[history["c3"]].deletions) == (4, 0)
    assert by_sha[history["c1"]].previous_blob_sha is None
    assert by_sha[history["c3"]].previous_blob_sha == by_sha[history["c1"]].blob_sha
    (png,) = [c for c in extraction.changes if c.path.endswith("logo.png")]
    assert png.additions is None and png.deletions is None


def test_versions_and_contents(extraction: Extraction) -> None:
    claude_versions = {v.content for v in extraction.versions if v.path == "CLAUDE.md"}
    assert claude_versions == {CLAUDE_V1, CLAUDE_V2, CLAUDE_V3, "AGENTS.md"}
    (symlink,) = [v for v in extraction.versions if v.path == "CLAUDE.md" and v.is_symlink]
    assert symlink.file_mode == "120000"
    (png,) = [v for v in extraction.versions if v.path.endswith("logo.png")]
    assert png.content is None and png.content_omitted is ContentOmission.BINARY
    # Every version of a deleted artifact is still stored.
    assert {v.content for v in extraction.versions if v.path.endswith("SKILL.md")} == {SKILL}
    # Vendored files never become artifacts.
    assert not any("node_modules" in v.path for v in extraction.versions)


def test_identities_use_mailmap_and_skip_merges(extraction: Extraction) -> None:
    identities = extraction.identities
    assert set(identities) == {"ana@example.org", "bob@example.org", "carol@example.org"}
    assert identities["bob@example.org"].name == "Bob"
    assert identities["bob@example.org"].commits == 2
    assert identities["ana@example.org"].commits == 4  # merge commit excluded
    assert extraction.monthly_activity == {date(2024, 1, 1): (8, 3)}


def test_commit_metadata_and_coauthors(extraction: Extraction, history: dict[str, str]) -> None:
    commits = {commit.sha: commit for commit in extraction.commits}
    first = commits[history["c1"]]
    assert first.coauthors == (("Claude", "noreply@anthropic.com"),)
    assert first.message.startswith("Initial commit")
    assert first.identity_key == "ana@example.org"


def test_contributor_activity_and_commit_files(
    extraction: Extraction, history: dict[str, str]
) -> None:
    activity = extraction.contributor_activity
    ana = {cat: counter for (key, cat), counter in activity.items() if key == "ana@example.org"}
    assert ana[FileCategory.AGENT_ARTIFACT].commits == 3  # c1, c3, c7
    assert ana[FileCategory.TEST].commits == 1
    assert ana[FileCategory.CODE].commits == 1
    bob = {cat: counter for (key, cat), counter in activity.items() if key == "bob@example.org"}
    assert bob[FileCategory.CODE].commits == 1
    assert bob[FileCategory.AGENT_ARTIFACT].commits == 1

    files_c3 = {f.path: f.file_category for f in extraction.commit_files if f.sha == history["c3"]}
    assert files_c3["tests/test_app.py"] is FileCategory.TEST
    assert files_c3["CLAUDE.md"] is FileCategory.AGENT_ARTIFACT
    files_c8 = {f.path: f.file_category for f in extraction.commit_files if f.sha == history["c8"]}
    assert files_c8["node_modules/pkg/CLAUDE.md"] is FileCategory.DOCS


def test_exact_rename_fallback_when_commit_is_too_large(
    history: dict[str, str], repo_builder: RepoBuilder, tmp_path: Path
) -> None:
    clone = GitRepository.clone(repo_builder.url, tmp_path / "clone", timeout_seconds=60)
    result = extract_repository(clone, MiningConfig(rename_candidate_limit=0))
    (nested,) = [c for c in result.changes if c.path == "packages/api/AGENTS.md"]
    assert nested.change_type is ChangeType.RENAMED
    assert nested.source_path == "packages/api/CLAUDE.md"


def test_empty_repository(repo_builder: RepoBuilder, tmp_path: Path) -> None:
    clone = GitRepository.clone(repo_builder.url, tmp_path / "clone", timeout_seconds=60)
    result = extract_repository(clone, MiningConfig())
    assert result.head_sha is None and result.artifacts == {}


def test_identity_key_and_content_decoding() -> None:
    assert identity_key("Ana", " Ana@Example.ORG ") == "ana@example.org"
    assert identity_key("Ana Dev", "") == "name:ana dev"
    assert decode_content("olá".encode(), 10) == ("olá", None)
    assert decode_content(b"x" * 11, 10) == (None, ContentOmission.TOO_LARGE)
    assert decode_content(b"\xff\xfe", 10) == (None, ContentOmission.NOT_UTF8)
    assert decode_content(b"a\0b", 10) == (None, ContentOmission.BINARY)


def test_git_version_requirement(monkeypatch) -> None:
    from agm_miner.git import repository
    from agm_miner.git.runner import GitError

    assert repository.git_version() >= (2, 0)
    monkeypatch.setattr(repository, "git_version", lambda: (2, 30, 1))
    with pytest.raises(GitError, match="2.44"):
        repository.ensure_git_version()


def test_artifact_introduced_only_by_a_merge_commit(repo_builder: RepoBuilder, tmp_path) -> None:
    """Observed in HuggingAGI/HuggingArxivLLM: no non-merge commit touches the artifact."""
    repo = repo_builder
    repo.commit("Init", {"src/app.py": "x = 1\n"}, date=day(1))
    repo.checkout("feature", create=True)
    repo.commit("Feature", {"src/feature.py": "y = 2\n"}, date=day(2))
    repo.checkout("main")
    repo.merge("feature", files={"AGENTS.md": "# Agents\n"}, date=day(3))

    clone = GitRepository.clone(repo.url, tmp_path / "clone", timeout_seconds=60)
    result = extract_repository(clone, MiningConfig())
    # The artifact exists at HEAD and its content is stored, but no change can be attributed
    # to a non-merge commit (merges carry no diff).
    assert result.artifacts["AGENTS.md"].exists_at_head
    assert [v.content for v in result.versions] == ["# Agents\n"]
    assert result.changes == [] and result.commits == []
