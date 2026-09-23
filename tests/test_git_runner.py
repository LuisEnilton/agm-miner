from __future__ import annotations

import os
import signal
import time

import pytest

from agm_miner.git.runner import GitRunner, terminate_active_processes
from agm_miner.runs import interrupt_signals

pytestmark = pytest.mark.git


def test_terminate_active_processes_kills_running_git(tmp_path) -> None:
    runner = GitRunner(None, timeout_seconds=60)
    process = runner.open_interactive(["cat-file", "--batch"])  # waits for input forever
    assert process.poll() is None
    assert terminate_active_processes() >= 1
    process.wait(timeout=5)
    assert process.returncode is not None
    assert terminate_active_processes() == 0


def test_git_runs_in_its_own_session() -> None:
    """A terminal Ctrl+C (sent to the foreground process group) must not reach git."""
    process = GitRunner(None, timeout_seconds=60).open_interactive(["cat-file", "--batch"])
    try:
        assert os.getpgid(process.pid) != os.getpgid(0)
    finally:
        process.kill()
        process.wait()


def test_sigterm_is_handled_like_ctrl_c() -> None:
    with pytest.raises(KeyboardInterrupt), interrupt_signals():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
    # The previous handler is restored afterwards.
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL


def test_ignored_sigint_is_restored_to_keyboard_interrupt() -> None:
    """Background jobs of non-interactive shells start with SIGINT ignored."""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with pytest.raises(KeyboardInterrupt), interrupt_signals():
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(5)
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous)
