"""Pure parsers for ``git`` output produced with ``-z`` (NUL-separated tokens).

NUL is the only byte that can appear neither in paths nor in commit messages, which makes these
formats unambiguous. All functions take an iterable of tokens so they work on streamed output.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime

NULL_SHA_RE = re.compile(r"^0+$")
_SHA_RE = re.compile(rb"^[0-9a-f]{40}([0-9a-f]{24})?$")
NUMSTAT_RE = re.compile(rb"^(\d+|-)\t(\d+|-)\t(.*)$", re.DOTALL)

GITLINK_MODE = "160000"
SYMLINK_MODE = "120000"


def decode(value: bytes) -> str:
    return value.decode("utf-8", "replace")


def is_null_sha(sha: str) -> bool:
    return bool(NULL_SHA_RE.match(sha))


# --------------------------------------------------------------------------- commit metadata

COMPACT_LOG_FORMAT = "%H%x00%P%x00%aN%x00%aE%x00%aI"
FULL_LOG_FORMAT = "%H%x00%P%x00%an%x00%ae%x00%aN%x00%aE%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"
_COMPACT_FIELDS = 5
_FULL_FIELDS = 11


@dataclass(frozen=True, slots=True)
class CompactCommit:
    sha: str
    parents: tuple[str, ...]
    author_name: str  # mailmap applied
    author_email: str  # mailmap applied
    authored_at: datetime

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


@dataclass(frozen=True, slots=True)
class FullCommit:
    sha: str
    parents: tuple[str, ...]
    raw_author_name: str
    raw_author_email: str
    author_name: str  # mailmap applied
    author_email: str  # mailmap applied
    authored_at: datetime
    committer_name: str
    committer_email: str
    committed_at: datetime
    message: str


def _chunks(tokens: Iterable[bytes], size: int) -> Iterator[list[bytes]]:
    """Group tokens into records of ``size`` fields.

    With ``-z``, ``git log`` terminates every record with a NUL, so the first field of each
    following record may carry a leading newline; empty trailing tokens are ignored.
    """
    record: list[bytes] = []
    for token in tokens:
        if not record:
            token = token.lstrip(b"\n")
            if not token:
                continue
        record.append(token)
        if len(record) == size:
            yield record
            record = []
    if record:
        raise ValueError(f"Truncated git log record: {record!r}")


def _parents(value: bytes) -> tuple[str, ...]:
    return tuple(decode(value).split()) if value else ()


def parse_compact_log(tokens: Iterable[bytes]) -> Iterator[CompactCommit]:
    for sha, parents, name, email, authored in _chunks(tokens, _COMPACT_FIELDS):
        yield CompactCommit(
            sha=decode(sha),
            parents=_parents(parents),
            author_name=decode(name),
            author_email=decode(email),
            authored_at=datetime.fromisoformat(decode(authored)),
        )


def parse_full_log(tokens: Iterable[bytes]) -> Iterator[FullCommit]:
    for record in _chunks(tokens, _FULL_FIELDS):
        (sha, parents, an, ae, amn, ame, authored, cn, ce, committed, message) = record
        yield FullCommit(
            sha=decode(sha),
            parents=_parents(parents),
            raw_author_name=decode(an),
            raw_author_email=decode(ae),
            author_name=decode(amn),
            author_email=decode(ame),
            authored_at=datetime.fromisoformat(decode(authored)),
            committer_name=decode(cn),
            committer_email=decode(ce),
            committed_at=datetime.fromisoformat(decode(committed)),
            message=decode(message),
        )


# --------------------------------------------------------------------------- raw diff entries


@dataclass(frozen=True, slots=True)
class RawChange:
    old_mode: str
    new_mode: str
    old_blob: str
    new_blob: str
    status: str  # A, M, D, T, R, C (U/X never appear for committed history)
    similarity: int | None
    path: str
    source_path: str | None = None

    @property
    def old_blob_or_none(self) -> str | None:
        return None if is_null_sha(self.old_blob) else self.old_blob

    @property
    def new_blob_or_none(self) -> str | None:
        return None if is_null_sha(self.new_blob) else self.new_blob


@dataclass(slots=True)
class NumstatEntry:
    path: str
    additions: int | None  # None for binary files
    deletions: int | None
    source_path: str | None = None


@dataclass(slots=True)
class CommitDiff:
    sha: str
    changes: list[RawChange] = field(default_factory=list)
    numstat: list[NumstatEntry] = field(default_factory=list)


def _parse_raw_header(token: bytes) -> tuple[str, str, str, str, str, int | None]:
    # ":100644 100644 <old> <new> M" / "R086"
    old_mode, new_mode, old_blob, new_blob, status = decode(token[1:]).split(" ")
    letter = status[0]
    similarity = int(status[1:]) if len(status) > 1 else None
    return old_mode, new_mode, old_blob, new_blob, letter, similarity


def _numstat_value(value: bytes) -> int | None:
    return None if value == b"-" else int(value)


def parse_diff_stream(tokens: Iterable[bytes]) -> Iterator[CommitDiff]:
    """Parse ``git log``/``git diff-tree`` output made of a commit id followed by ``--raw`` and/or
    ``--numstat`` entries (with ``-z``). Commits without entries (e.g. merges) are yielded too."""
    iterator = iter(tokens)
    current: CommitDiff | None = None
    for token in iterator:
        token = token.lstrip(b"\n")
        if not token:
            continue
        if token.startswith(b":"):
            if current is None:
                raise ValueError("Raw diff entry before any commit id")
            old_mode, new_mode, old_blob, new_blob, letter, similarity = _parse_raw_header(token)
            if letter in ("R", "C"):
                source = decode(next(iterator))
                path = decode(next(iterator))
            else:
                source = None
                path = decode(next(iterator))
            current.changes.append(
                RawChange(old_mode, new_mode, old_blob, new_blob, letter, similarity, path, source)
            )
            continue
        numstat = NUMSTAT_RE.match(token)
        if numstat is not None:
            if current is None:
                raise ValueError("Numstat entry before any commit id")
            additions, deletions, path_bytes = numstat.groups()
            if path_bytes:
                entry = NumstatEntry(
                    decode(path_bytes), _numstat_value(additions), _numstat_value(deletions)
                )
            else:
                source = decode(next(iterator))
                entry = NumstatEntry(
                    decode(next(iterator)),
                    _numstat_value(additions),
                    _numstat_value(deletions),
                    source_path=source,
                )
            current.numstat.append(entry)
            continue
        if _SHA_RE.match(token):
            if current is not None:
                yield current
            current = CommitDiff(sha=decode(token))
            continue
        raise ValueError(f"Unexpected token in git diff output: {token[:80]!r}")
    if current is not None:
        yield current


# --------------------------------------------------------------------------- trees and blobs


@dataclass(frozen=True, slots=True)
class TreeEntry:
    mode: str
    object_type: str
    sha: str
    path: str


def parse_ls_tree(tokens: Iterable[bytes]) -> Iterator[TreeEntry]:
    """Parse ``git ls-tree -r -z`` output: ``<mode> SP <type> SP <sha> TAB <path>``."""
    for token in tokens:
        if not token:
            continue
        meta, path = token.split(b"\t", 1)
        mode, object_type, sha = decode(meta).split(" ")
        yield TreeEntry(mode, object_type, sha, decode(path))


# --------------------------------------------------------------------------- trailers

_COAUTHOR_RE = re.compile(
    r"^\s*co-authored-by:\s*(?P<name>.*?)\s*<(?P<email>[^>]*)>\s*$", re.I | re.M
)
_NOREPLY_RE = re.compile(
    r"^(?:\d+\+)?(?P<login>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?)"
    r"@users\.noreply\.github\.com$",
    re.I,
)


def parse_coauthors(message: str) -> list[tuple[str, str]]:
    """``Co-authored-by: Name <email>`` trailers, in order, without exact duplicates."""
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for match in _COAUTHOR_RE.finditer(message):
        pair = (match.group("name"), match.group("email").strip())
        if pair not in seen:
            seen.add(pair)
            result.append(pair)
    return result


def github_login_from_email(email: str) -> str | None:
    match = _NOREPLY_RE.match(email.strip())
    return match.group("login") if match else None
