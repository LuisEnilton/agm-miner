"""Thin, reproducible wrapper around the ``git`` executable.

* The user's global/system git configuration is ignored (``GIT_CONFIG_GLOBAL=/dev/null``) so
  aliases, colours or diff drivers can never change the parsed output.
* Output is streamed as NUL-separated tokens (all commands run with ``-z``), so histories with
  hundreds of thousands of commits never need to fit in memory at once.
* Input for ``--stdin`` commands is written from a separate thread to avoid pipe deadlocks.
* Every command has a wall-clock timeout enforced by a watchdog.
* Commands run in their own session, so a Ctrl+C in the terminal does not kill them halfway;
  :func:`terminate_active_processes` stops them on purpose when a run is aborted.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

_READ_CHUNK = 1 << 20

_active_processes: set[subprocess.Popen[bytes]] = set()
_active_lock = threading.Lock()


def _spawn(command: list[str], **kwargs) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(command, env=git_environment(), start_new_session=True, **kwargs)
    with _active_lock:
        for finished in [p for p in _active_processes if p.poll() is not None]:
            _active_processes.discard(finished)
        _active_processes.add(process)
    return process


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the whole process group of a git command (each one leads its own session).

    ``git clone`` spawns ``index-pack`` and friends; killing only the leader leaves them
    running with the output pipes open, so whoever reads the pipes would wait for them forever.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:  # pragma: no cover - the group is always ours
        process.kill()


def terminate_active_processes() -> int:
    """Kill every git process still running (used when a run is aborted); returns how many."""
    with _active_lock:
        running = [process for process in _active_processes if process.poll() is None]
        _active_processes.clear()
    for process in running:
        _kill_group(process)
    return len(running)


class GitError(RuntimeError):
    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(args)} failed with exit code {returncode}: {stderr.strip()}"
        )
        self.args_list = args
        self.returncode = returncode
        self.stderr = stderr


class GitTimeoutError(GitError):
    pass


def git_environment() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "LC_ALL": "C",
        }
    )
    return env


_BASE_OPTIONS = (
    "-c",
    "color.ui=never",
    "-c",
    "core.quotePath=false",
    "-c",
    "log.showSignature=false",
)


class GitRunner:
    def __init__(self, repo_dir: Path | None, timeout_seconds: int) -> None:
        self.repo_dir = repo_dir
        self.timeout_seconds = timeout_seconds
        # Extra ``-c key=value`` options applied to every subsequent command.
        self.config: dict[str, str] = {}

    def _command(self, args: list[str]) -> list[str]:
        command = ["git", *_BASE_OPTIONS]
        for key, value in self.config.items():
            command += ["-c", f"{key}={value}"]
        if self.repo_dir is not None:
            command += ["-C", str(self.repo_dir)]
        return command + args

    def run(self, args: list[str], stdin: bytes | None = None) -> bytes:
        """Run a command and return its whole stdout (for small outputs)."""
        process = _spawn(
            self._command(args),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout, stderr = process.communicate(input=stdin, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _kill_group(process)
            process.communicate()
            raise GitTimeoutError(args, -1, f"timed out after {self.timeout_seconds}s") from exc
        if process.returncode != 0:
            raise GitError(args, process.returncode, stderr.decode("utf-8", "replace"))
        return stdout

    def stream_tokens(
        self, args: list[str], stdin_lines: Iterable[str] | None = None
    ) -> Iterator[bytes]:
        """Yield the NUL-separated tokens of a command's stdout as they are produced."""
        with self._popen(args, stdin_lines) as process:
            pending = b""
            assert process.stdout is not None
            while chunk := process.stdout.read(_READ_CHUNK):
                pending += chunk
                *tokens, pending = pending.split(b"\0")
                yield from tokens
            if pending:
                yield pending

    def _popen(self, args: list[str], stdin_lines: Iterable[str] | None) -> _ManagedProcess:
        return _ManagedProcess(self._command(args), args, stdin_lines, self.timeout_seconds)

    def open_interactive(self, args: list[str]) -> subprocess.Popen[bytes]:
        """Start a request/response command such as ``cat-file --batch``."""
        return _spawn(
            self._command(args),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )


class _ManagedProcess:
    """Popen with a stdin writer thread, a stderr drain thread and a timeout watchdog."""

    def __init__(
        self,
        command: list[str],
        args: list[str],
        stdin_lines: Iterable[str] | None,
        timeout_seconds: int,
    ) -> None:
        self._args = args
        self._stdin_lines = stdin_lines
        self._timeout_seconds = timeout_seconds
        self._timed_out = threading.Event()
        self._stderr_chunks: list[bytes] = []
        self.process = _spawn(
            command,
            stdin=subprocess.PIPE if stdin_lines is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> subprocess.Popen[bytes]:
        if self._stdin_lines is not None:
            self._start(self._write_stdin, self.process.stdin, self._stdin_lines)
        self._start(self._drain, self.process.stderr)
        self._watchdog = threading.Timer(self._timeout_seconds, self._kill)
        self._watchdog.daemon = True
        self._watchdog.start()
        return self.process

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc_type is not None:
            self._kill()
        self.process.wait()
        self._watchdog.cancel()
        for thread in self._threads:
            thread.join()
        if self.process.stdout is not None:
            self.process.stdout.close()
        if exc_type is not None:
            return
        stderr = b"".join(self._stderr_chunks).decode("utf-8", "replace")
        if self._timed_out.is_set():
            raise GitTimeoutError(self._args, -1, f"timed out after {self._timeout_seconds}s")
        if self.process.returncode != 0:
            raise GitError(self._args, self.process.returncode, stderr)

    def _start(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    @staticmethod
    def _write_stdin(stream: IO[bytes], lines: Iterable[str]) -> None:
        # git may exit (or fail) before reading all of its input; that is reported via its
        # exit status, so a closed pipe here is not an error in itself.
        with contextlib.suppress(BrokenPipeError):
            try:
                for line in lines:
                    stream.write(line.encode("utf-8") + b"\n")
            finally:
                stream.close()

    def _drain(self, stream: IO[bytes]) -> None:
        for chunk in iter(lambda: stream.read(65536), b""):
            self._stderr_chunks.append(chunk)
        stream.close()

    def _kill(self) -> None:
        if self.process.poll() is None:
            self._timed_out.set()
        _kill_group(self.process)
