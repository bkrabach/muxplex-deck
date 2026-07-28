"""`InstanceLock` -- the single-instance guard that stops two sidecar
processes from racing on one exclusive HID device and one shared status
file.

See AGENTS.md / WINDOWS_NATIVE_SPEC.md for the incident this exists to
close: a single `schtasks /Run` produced two live `muxplex_deck run`
processes on real Windows hardware. Regardless of the exact Task Scheduler
mechanism (unverifiable from this Linux host -- see the module docstring
in `singleton.py`), two sidecars racing on one exclusive resource is a bug
on every platform, so the guard lives in the process itself.

No hardware, no real subprocess, no real service manager: this is a pure
filesystem-lock test using `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muxplex_deck.singleton import InstanceLock, InstanceLockError, default_lock_path


class TestInstanceLock:
    def test_first_acquire_succeeds_and_creates_the_lock_file(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "nested" / "muxplex-deck.lock"
        lock = InstanceLock(path)
        lock.acquire()
        try:
            assert path.exists()
        finally:
            lock.release()

    def test_second_instance_on_the_same_path_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "muxplex-deck.lock"
        first = InstanceLock(path)
        first.acquire()
        try:
            second = InstanceLock(path)
            with pytest.raises(InstanceLockError):
                second.acquire()
        finally:
            first.release()

    def test_second_instance_never_holds_the_handle_after_failing(
        self, tmp_path: Path
    ) -> None:
        """A failed acquire must not leak an open, unlocked file handle."""
        path = tmp_path / "muxplex-deck.lock"
        first = InstanceLock(path)
        first.acquire()
        try:
            second = InstanceLock(path)
            with pytest.raises(InstanceLockError):
                second.acquire()
            assert second._fh is None  # white-box: a failed acquire leaks no handle
        finally:
            first.release()

    def test_release_then_reacquire_succeeds_immediately(self, tmp_path: Path) -> None:
        """The v0.5.3 restart contract: once the old instance is gone, a

        replacement must acquire the lock immediately -- no stale-lock
        window, no PID-liveness heuristic to get wrong.
        """
        path = tmp_path / "muxplex-deck.lock"
        first = InstanceLock(path)
        first.acquire()
        first.release()

        second = InstanceLock(path)
        second.acquire()  # must not raise
        second.release()

    def test_context_manager_releases_on_normal_exit(self, tmp_path: Path) -> None:
        path = tmp_path / "muxplex-deck.lock"
        with InstanceLock(path):
            blocked = InstanceLock(path)
            with pytest.raises(InstanceLockError):
                blocked.acquire()

        after = InstanceLock(path)
        after.acquire()  # released now that the `with` block exited
        after.release()

    def test_context_manager_releases_even_on_exception(self, tmp_path: Path) -> None:
        path = tmp_path / "muxplex-deck.lock"
        with pytest.raises(RuntimeError), InstanceLock(path):
            raise RuntimeError("boom")

        after = InstanceLock(path)
        after.acquire()  # must not raise despite the exception above
        after.release()

    def test_reacquiring_on_the_same_instance_is_a_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "muxplex-deck.lock"
        lock = InstanceLock(path)
        lock.acquire()
        lock.acquire()  # must not raise or deadlock
        lock.release()

    def test_release_without_acquire_is_a_noop(self, tmp_path: Path) -> None:
        InstanceLock(tmp_path / "never-acquired.lock").release()  # must not raise

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "muxplex-deck.lock"
        lock = InstanceLock(path)
        lock.acquire()
        lock.release()
        lock.release()  # must not raise the second time


class TestDefaultLockPath:
    def test_lives_under_the_xdg_state_status_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One state directory, not two -- alongside `status.json`."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        path = default_lock_path()
        assert path.parent.name == "muxplex-deck"
        assert path.name == "muxplex-deck.lock"
