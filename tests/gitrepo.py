"""Builder for small, fully deterministic git repositories used as mining fixtures."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

DEFAULT_AUTHOR = ("Ana Dev", "ana@example.org")


class RepoBuilder:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q", "-b", "main")
        # Allow partial clones (--filter) and fetching arbitrary objects over file://.
        self._git("config", "uploadpack.allowFilter", "true")
        self._git("config", "uploadpack.allowAnySHA1InWant", "true")

    @property
    def url(self) -> str:
        return self.path.resolve().as_uri()

    def _env(self, author: tuple[str, str], date: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": author[0],
                "GIT_AUTHOR_EMAIL": author[1],
                "GIT_COMMITTER_NAME": author[0],
                "GIT_COMMITTER_EMAIL": author[1],
                "GIT_AUTHOR_DATE": date,
                "GIT_COMMITTER_DATE": date,
            }
        )
        return env

    def _git(self, *args: str, env: dict[str, str] | None = None) -> str:
        completed = subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env or self._env(DEFAULT_AUTHOR, "2024-01-01T00:00:00+00:00"),
        )
        return completed.stdout.strip()

    def write(self, files: dict[str, str | bytes | None]) -> None:
        for relative, content in files.items():
            target = self.path / relative
            if content is None:
                self._git("rm", "-q", "--", relative)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                target.unlink()
            if isinstance(content, bytes):
                target.write_bytes(content)
            else:
                target.write_text(content, encoding="utf-8")
            self._git("add", "--", relative)

    def symlink(self, relative: str, target: str) -> None:
        link = self.path / relative
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        self._git("add", "--", relative)

    def move(self, source: str, destination: str) -> None:
        (self.path / destination).parent.mkdir(parents=True, exist_ok=True)
        self._git("mv", source, destination)

    def commit(
        self,
        message: str,
        files: dict[str, str | bytes | None] | None = None,
        *,
        author: tuple[str, str] = DEFAULT_AUTHOR,
        date: str = "2024-01-01T00:00:00+00:00",
    ) -> str:
        if files:
            self.write(files)
        self._git("commit", "-q", "--allow-empty", "-m", message, env=self._env(author, date))
        return self._git("rev-parse", "HEAD")

    def checkout(self, branch: str, *, create: bool = False) -> None:
        if create:
            self._git("checkout", "-q", "-b", branch)
        else:
            self._git("checkout", "-q", branch)

    def merge(
        self,
        branch: str,
        *,
        author: tuple[str, str] = DEFAULT_AUTHOR,
        date: str = "2024-01-01T00:00:00+00:00",
    ) -> str:
        self._git("merge", "-q", "--no-ff", "--no-edit", branch, env=self._env(author, date))
        return self._git("rev-parse", "HEAD")

    def head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def blob(self, revision: str, path: str) -> str:
        return self._git("rev-parse", f"{revision}:{path}")
