"""High-level read operations on a blobless (partial) clone of a repository's default branch."""

from __future__ import annotations

import logging
import re
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from agm_miner.git.parsing import (
    COMPACT_LOG_FORMAT,
    FULL_LOG_FORMAT,
    NUMSTAT_RE,
    CommitDiff,
    CompactCommit,
    FullCommit,
    TreeEntry,
    parse_compact_log,
    parse_diff_stream,
    parse_full_log,
    parse_ls_tree,
)
from agm_miner.git.runner import GitError, GitRunner

logger = logging.getLogger(__name__)

_COMMON_DIFF_OPTIONS = ["--no-abbrev", "--no-ext-diff", "--no-textconv", "-r"]
# `git --no-lazy-fetch` (used to inspect a partial clone without downloading) needs git 2.44.
MINIMUM_GIT_VERSION = (2, 44)


def git_version() -> tuple[int, ...]:
    output = GitRunner(None, 60).run(["version"]).decode()  # "git version 2.51.0"
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", output)
    if match is None:
        raise GitError(["version"], -1, f"cannot parse {output!r}")
    return tuple(int(part) for part in match.groups() if part is not None)


def ensure_git_version(minimum: tuple[int, int] = MINIMUM_GIT_VERSION) -> None:
    found = git_version()
    if found[:2] < minimum:
        required = ".".join(map(str, minimum))
        raise GitError(["version"], -1, f"git >= {required} is required, found {found}")


@dataclass(frozen=True, slots=True)
class Blob:
    sha: str
    size: int
    data: bytes | None  # None when the object is missing


class GitRepository:
    def __init__(self, path: Path, timeout_seconds: int) -> None:
        self.path = path
        self.git = GitRunner(path, timeout_seconds)

    @classmethod
    def clone(cls, url: str, destination: Path, timeout_seconds: int) -> GitRepository:
        """Blobless, single-branch clone without checkout: commits and trees only; blobs are
        fetched on demand (see :meth:`prefetch_blobs`)."""
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        GitRunner(None, timeout_seconds).run(
            [
                "clone",
                "--quiet",
                "--filter=blob:none",
                "--no-checkout",
                "--single-branch",
                "--no-tags",
                url,
                str(destination),
            ]
        )
        return cls(destination, timeout_seconds)

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    # ------------------------------------------------------------------ refs

    def head_sha(self) -> str | None:
        """Commit of HEAD, or ``None`` for an empty repository."""
        try:
            return self.git.run(["rev-parse", "--verify", "--quiet", "HEAD"]).decode().strip()
        except GitError as exc:
            if exc.returncode == 1:
                return None
            raise

    def enable_mailmap(self, commit: str, batch_size: int) -> bool:
        """Use the ``.mailmap`` committed at ``commit``.

        A clone without checkout has no working tree, so git would silently ignore the
        repository's ``.mailmap``; point ``mailmap.blob`` at the committed file instead.
        """
        entries = [
            entry
            for entry in parse_ls_tree(
                self.git.stream_tokens(["ls-tree", "-z", commit, "--", ".mailmap"])
            )
            if entry.object_type == "blob"
        ]
        if not entries:
            return False
        self.prefetch_blobs([entries[0].sha], batch_size)
        self.git.config["mailmap.blob"] = entries[0].sha
        return True

    # ------------------------------------------------------------------ history

    def iter_compact_log(self) -> Iterator[CompactCommit]:
        """Every commit reachable from HEAD (mailmap applied to author name/e-mail)."""
        return parse_compact_log(
            self.git.stream_tokens(["log", "-z", "--use-mailmap", f"--format={COMPACT_LOG_FORMAT}"])
        )

    def iter_raw_log(self) -> Iterator[CommitDiff]:
        """Raw file changes of every non-merge commit reachable from HEAD, without rename
        detection (renames need blob contents, which a blobless clone does not have)."""
        return parse_diff_stream(
            self.git.stream_tokens(
                ["log", "-z", "--raw", "--no-renames", *_COMMON_DIFF_OPTIONS, "--format=%H"]
            )
        )

    def iter_full_commits(self, shas: Iterable[str]) -> Iterator[FullCommit]:
        shas = list(shas)
        if not shas:
            # `git log --stdin` with no input falls back to HEAD.
            return iter(())
        return parse_full_log(
            self.git.stream_tokens(
                ["log", "-z", "--no-walk=unsorted", "--stdin", f"--format={FULL_LOG_FORMAT}"],
                stdin_lines=shas,
            )
        )

    def iter_diff_tree(
        self,
        shas: Iterable[str],
        *,
        detect_renames: bool,
        raw: bool,
        numstat: bool,
        pathspecs: Iterable[str] = (),
    ) -> Iterator[CommitDiff]:
        """``git diff-tree`` of each commit against its first parent (root commits against the
        empty tree). Needs the blobs of the compared files for rename detection and numstat."""
        shas = list(shas)
        if not shas:
            return iter(())
        args = ["diff-tree", "--stdin", "-z", "--root", *_COMMON_DIFF_OPTIONS]
        args += ["-M", "-C"] if detect_renames else ["--no-renames"]
        if raw:
            args.append("--raw")
        if numstat:
            args.append("--numstat")
        specs = list(pathspecs)
        if specs:
            args += ["--", *specs]
        return parse_diff_stream(self.git.stream_tokens(args, stdin_lines=shas))

    def blob_numstat(self, old_blob: str, new_blob: str) -> tuple[int | None, int | None]:
        """Lines added/removed between two blobs (``None`` for binary content)."""
        output = self.git.run(
            ["diff", "--numstat", "-z", "--no-ext-diff", "--no-textconv", old_blob, new_blob]
        )
        match = NUMSTAT_RE.match(output.split(b"\0", 1)[0])
        if match is None:
            return 0, 0  # identical content produces no output
        additions, deletions, _ = match.groups()
        return (
            None if additions == b"-" else int(additions),
            None if deletions == b"-" else int(deletions),
        )

    def ls_tree(self, treeish: str = "HEAD") -> Iterator[TreeEntry]:
        return parse_ls_tree(
            self.git.stream_tokens(["ls-tree", "-r", "-z", "--full-tree", treeish])
        )

    # ------------------------------------------------------------------ blobs

    def prefetch_blobs(self, shas: Iterable[str], batch_size: int) -> None:
        """Download missing blobs from the promisor remote in batches (one round trip per batch
        instead of one per object, which is what lazy fetching would do)."""
        missing = self._missing_objects(shas)
        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            self.git.run(
                [
                    "-c",
                    "fetch.negotiationAlgorithm=noop",
                    "fetch",
                    "--quiet",
                    "origin",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--recurse-submodules=no",
                    "--filter=blob:none",
                    "--stdin",
                ],
                stdin="\n".join(batch).encode() + b"\n",
            )
        if missing:
            logger.debug("Prefetched %s blobs into %s", len(missing), self.path)

    def _missing_objects(self, shas: Iterable[str]) -> list[str]:
        unique = sorted(set(shas))
        if not unique:
            return []
        # --no-lazy-fetch: the check itself must not download the objects one by one. Objects
        # absent locally are reported as "<sha> missing".
        output = self.git.run(
            ["--no-lazy-fetch", "cat-file", "--batch-check=%(objectname)", "--buffer"],
            stdin=("\n".join(unique) + "\n").encode(),
        )
        missing = []
        for line in output.decode().splitlines():
            if line.endswith(" missing"):
                missing.append(line.split(" ", 1)[0])
        return missing

    def read_blobs(self, shas: Iterable[str]) -> Iterator[Blob]:
        """Stream blob contents with ``git cat-file --batch`` (in request order)."""
        requested = list(dict.fromkeys(shas))
        if not requested:
            return
        process = self.git.open_interactive(["cat-file", "--batch"])
        assert process.stdin is not None and process.stdout is not None
        try:
            for sha in requested:
                process.stdin.write(sha.encode() + b"\n")
                process.stdin.flush()
                header = process.stdout.readline().decode().rstrip("\n")
                parts = header.split(" ")
                if len(parts) == 2 and parts[1] == "missing":
                    yield Blob(sha, 0, None)
                    continue
                if len(parts) != 3:
                    raise GitError(["cat-file", "--batch"], -1, f"unexpected header {header!r}")
                size = int(parts[2])
                data = process.stdout.read(size)
                process.stdout.read(1)  # trailing LF
                yield Blob(sha, size, data)
        finally:
            process.stdin.close()
            process.stdout.close()
            process.wait()


def remote_head(url: str, timeout_seconds: int) -> str | None:
    """Commit the remote's HEAD points to, without cloning (``None`` for an empty repository)."""
    output = GitRunner(None, timeout_seconds).run(["ls-remote", url, "HEAD"]).decode()
    for line in output.splitlines():
        sha, _, ref = line.partition("\t")
        if ref == "HEAD":
            return sha
    return None
