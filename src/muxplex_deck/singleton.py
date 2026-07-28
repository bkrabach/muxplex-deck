"""Single-instance guard -- prevents two sidecar processes from racing on
one exclusive HID device and one shared status file.

Why this exists: the Windows Task Scheduler double-spawn incident (see
AGENTS.md / WINDOWS_NATIVE_SPEC.md) showed two `muxplex_deck run` processes
launched from what should have been a single `schtasks /Run`. The task XML
already asks Task Scheduler for `MultipleInstancesPolicy=IgnoreNew`, but
that policy is enforced by Task Scheduler's own bookkeeping of "is an
instance of this task currently running" -- and that bookkeeping is not
something this process can see or trust. The same failure shape can happen
for reasons that have nothing to do with Task Scheduler at all: a user runs
`muxplex-deck run` manually while the service is already up, double-clicks
a shortcut, or a stale process survives an unclean shutdown. Whatever the
trigger, two sidecars fighting over one exclusive HID handle and one shared
`status.json` is a bug regardless of platform, so the guard lives here,
inside the process itself, rather than depending on any service manager's
promise.

Mechanism: an OS-level advisory lock (`fcntl.flock` on POSIX,
`msvcrt.locking` on Windows) held on an open file handle. Both are enforced
by the kernel against the *file descriptor*, not against a value written to
disk -- there is deliberately no PID recorded in the lock file and no
staleness check. That is what makes this safe for the legitimate restart
case the v0.5.3 PID contract depends on: when the old process exits, by
any means (clean exit, Ctrl+C, `SIGTERM`, `schtasks /End`'s
`TerminateProcess`, or a hard `SIGKILL`), the OS closes its file handles as
part of process teardown and the lock is released immediately -- there is
no stale-lock file for a replacement instance to get stuck behind, and
no PID-liveness heuristic that could itself race. A PID-in-a-file scheme
would need exactly that kind of staleness detection (`os.kill(pid, 0)`
or equivalent) and could still lose the race; asking the kernel "is this
fd currently locked" has no such window.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import IO, Self

_LOCK_FILE_NAME = "muxplex-deck.lock"


class InstanceLockError(RuntimeError):
    """Raised by `InstanceLock.acquire()` when another instance holds the lock."""


class InstanceLock:
    """A non-blocking, OS-enforced exclusive lock scoped to one file path.

    Not reentrant and not thread-safe by design -- `main.run()` acquires it
    once, at most once, on its own thread, before entering the hotplug
    loop. Safe to construct repeatedly (each test gets its own instance);
    `acquire()` on an already-acquired instance is a no-op.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        """Acquire the lock or raise `InstanceLockError`. Never blocks.

        Idempotent: calling this again while already held is a no-op --
        it does not attempt to re-lock (which would be a harmless no-op
        on POSIX `flock` but can raise on Windows `msvcrt.locking`).
        """
        if self._fh is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+b")  # noqa: SIM115 -- handle outlives this call; closed in release()
        try:
            # msvcrt.locking() locks a byte range starting at the file's
            # CURRENT position; an empty file has no bytes to lock, so
            # ensure at least one exists before seeking back to it. This
            # content is never read -- the lock's only meaning is "is this
            # fd currently held", never anything written to disk.
            fh.seek(0, 2)
            if fh.tell() == 0:
                fh.write(b"l")
                fh.flush()
            fh.seek(0)
            _lock_exclusive_nonblocking(fh)
        except OSError as exc:
            fh.close()
            raise InstanceLockError(
                f"another muxplex-deck instance already holds the lock at {self._path}"
            ) from exc
        self._fh = fh

    def release(self) -> None:
        """Release the lock and close the handle. Never raises."""
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        with contextlib.suppress(OSError):
            fh.seek(0)
            _unlock(fh)
        with contextlib.suppress(OSError):
            fh.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def _lock_exclusive_nonblocking(fh: IO[bytes]) -> None:
    """Platform-dispatching non-blocking exclusive lock on `fh`'s first byte.

    Raises `OSError` (or a subclass) if another process already holds it --
    callers translate that into `InstanceLockError`.
    """
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh: IO[bytes]) -> None:
    """Platform-dispatching unlock, mirroring `_lock_exclusive_nonblocking`."""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def default_lock_path() -> Path:
    """`<status_dir>/muxplex-deck.lock` -- one state directory, not two.

    Reuses `statusfile.default_status_dir()` (itself `$XDG_STATE_HOME`-aware
    and already redirected under test by `tests/conftest.py`'s Rail 3) so
    this needs no separate env var or test rail of its own.
    """
    from .statusfile import default_status_dir

    return default_status_dir() / _LOCK_FILE_NAME
