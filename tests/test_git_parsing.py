from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from agm_miner.git.parsing import (
    github_login_from_email,
    parse_coauthors,
    parse_compact_log,
    parse_diff_stream,
    parse_full_log,
    parse_ls_tree,
)

SHA1 = "a" * 40
SHA2 = "b" * 40
BLOB1 = "1" * 40
BLOB2 = "2" * 40
ZERO = "0" * 40


def tokens(raw: bytes) -> list[bytes]:
    return raw.split(b"\0")


def test_compact_log() -> None:
    raw = (
        f"{SHA1}\0{SHA2} {BLOB1}\0Ana\0ana@x.org\0"
        "2024-05-01T10:00:00-03:00\0"
        f"{SHA2}\0\0Bob\0bob@x.org\0"
        "2024-04-30T09:00:00+00:00\0"
    ).encode()
    commits = list(parse_compact_log(tokens(raw)))
    assert [c.sha for c in commits] == [SHA1, SHA2]
    assert commits[0].is_merge and not commits[1].is_merge
    assert commits[1].parents == ()
    assert commits[0].authored_at == datetime(2024, 5, 1, 10, tzinfo=timezone(timedelta(hours=-3)))


def test_full_log_keeps_multiline_messages() -> None:
    message = "Subject\n\nBody line\nCo-authored-by: Claude <noreply@anthropic.com>\n"
    raw = (
        f"{SHA1}\0{SHA2}\0ana\0ANA@X.ORG\0Ana Dev\0ana@x.org\0"
        "2024-05-01T10:00:00+00:00\0"
        "GitHub\0noreply@github.com\0"
        f"2024-05-02T10:00:00+00:00\0{message}\0"
    ).encode()
    (commit,) = parse_full_log(tokens(raw))
    assert commit.raw_author_email == "ANA@X.ORG"
    assert commit.author_name == "Ana Dev"
    assert commit.committer_name == "GitHub"
    assert commit.committed_at == datetime(2024, 5, 2, 10, tzinfo=UTC)
    assert commit.message == message


def test_truncated_log_record_is_an_error() -> None:
    with pytest.raises(ValueError, match="Truncated"):
        list(parse_compact_log([SHA1.encode(), b""]))


def test_raw_diff_stream_with_merge_and_rename() -> None:
    raw = (
        f"{SHA1}\0"  # merge commit: no entries
        f"{SHA2}\0\n"
        f":000000 100644 {ZERO} {BLOB1} A\0new file.md\0"
        f":100644 120000 {BLOB1} {BLOB2} T\0CLAUDE.md\0"
        f":100644 100644 {BLOB1} {BLOB2} R087\0old/AGENTS.md\0new/AGENTS.md\0"
    ).encode()
    diffs = list(parse_diff_stream(tokens(raw)))
    assert [d.sha for d in diffs] == [SHA1, SHA2]
    assert diffs[0].changes == []
    added, typechange, renamed = diffs[1].changes
    assert added.status == "A" and added.path == "new file.md"
    assert added.old_blob_or_none is None and added.new_blob_or_none == BLOB1
    assert typechange.status == "T" and typechange.new_mode == "120000"
    assert renamed.status == "R" and renamed.similarity == 87
    assert renamed.source_path == "old/AGENTS.md" and renamed.path == "new/AGENTS.md"


def test_numstat_stream_including_binary_and_renames() -> None:
    raw = (
        f"{SHA1}\0\n3\t1\tCLAUDE.md\0-\t-\tlogo.png\0"
        f"2\t0\t\0old.md\0AGENTS.md\0"
        f"{SHA2}\0\n1\t0\tpath\twith\ttabs.md\0"
    ).encode()
    first, second = parse_diff_stream(tokens(raw))
    assert [(e.path, e.additions, e.deletions) for e in first.numstat] == [
        ("CLAUDE.md", 3, 1),
        ("logo.png", None, None),
        ("AGENTS.md", 2, 0),
    ]
    assert first.numstat[2].source_path == "old.md"
    assert second.numstat[0].path == "path\twith\ttabs.md"


def test_diff_stream_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        list(parse_diff_stream([b"not a sha"]))


def test_ls_tree() -> None:
    raw = f"100644 blob {BLOB1}\tCLAUDE.md\x00160000 commit {BLOB2}\tvendor/sub\x00".encode()
    entries = list(parse_ls_tree(tokens(raw)))
    assert [(e.mode, e.object_type, e.path) for e in entries] == [
        ("100644", "blob", "CLAUDE.md"),
        ("160000", "commit", "vendor/sub"),
    ]


def test_coauthor_trailers() -> None:
    message = (
        "Add CLAUDE.md\n\n"
        "Co-Authored-By: Claude Opus <noreply@anthropic.com>\n"
        "co-authored-by: Bob <bob@x.org>\n"
        "Co-authored-by: Bob <bob@x.org>\n"
        "Co-authored-by: broken line without email\n"
    )
    assert parse_coauthors(message) == [
        ("Claude Opus", "noreply@anthropic.com"),
        ("Bob", "bob@x.org"),
    ]


@pytest.mark.parametrize(
    ("email", "login"),
    [
        ("12345+octo-cat@users.noreply.github.com", "octo-cat"),
        ("octocat@users.noreply.github.com", "octocat"),
        ("49699333+dependabot[bot]@users.noreply.github.com", "dependabot[bot]"),
        ("ana@example.org", None),
        ("noreply@github.com", None),
    ],
)
def test_github_login_from_email(email: str, login: str | None) -> None:
    assert github_login_from_email(email) == login
