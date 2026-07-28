"""Sidecar status file -- what the running process is doing, published for
`muxplex-deck status` to read without contending for the exclusive HID
handle.

HID access is exclusive: once the sidecar (or the service running it) has
the Stream Deck open, a second process trying to open the same device gets
a false "could not open" failure -- see `cli.check_hid_openable`'s
docstring. A naive `status` command that probed the device directly would
hit exactly the same wall. Instead, the RUNNING sidecar periodically writes
what it knows (device capabilities, server reachability, current
session/view/page) to a small JSON file, and `status` just reads that file.

Location: `$XDG_STATE_HOME/muxplex-deck/status.json`, default
`~/.local/state/muxplex-deck/status.json` (dir created `0700`).

Atomic write: temp file in the *same* directory, then `os.replace()` --
POSIX guarantees `rename()`/`replace()` is atomic, so a reader never
observes a partially written file, no matter when it happens to read.

Best-effort: `write_status`/`StatusReporter.update` never raise. A write
failure is logged and swallowed -- the sidecar's poll loop must keep
running even if it can't publish its status (e.g. read-only filesystem,
disk full).

Never contains secrets: nothing here accepts or stores the federation key/
Bearer token. Only add fields that are safe to leave world-readable... well,
0700-directory-readable, but still: no credentials, ever.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("muxplex_deck")

SCHEMA_VERSION = 1

_STATUS_DIR_NAME = "muxplex-deck"
_STATUS_FILE_NAME = "status.json"

# Windows only: os.replace() raises PermissionError when the destination is
# open by another process -- and cli.status()/_wait_for_fresh_status() read
# this exact file while the sidecar is writing it. The blanket `except
# Exception` below already degrades a dropped write to a logged warning
# (never a crash), but a dropped write during the restart-wait window could
# manufacture exactly the false "has not published fresh status" alarm
# v0.5.3 was released to eliminate -- see WINDOWS_NATIVE_SPEC.md section
# 3.4. POSIX `rename()`/`replace()` has no such failure mode (an open file
# descriptor doesn't block a rename), so this retry is Windows-only and
# does not change behavior anywhere else.
_WIN_REPLACE_RETRY_ATTEMPTS = 3
_WIN_REPLACE_RETRY_DELAY_SECONDS = 0.05


def _replace_with_windows_retry(src: str, dst: Path) -> None:
    """`os.replace(src, dst)`, retrying on Windows if the destination is locked.

    Elsewhere this is just `os.replace(src, dst)` -- no retry, no delay,
    byte-for-byte the previous behavior. On `win32`, retries up to
    `_WIN_REPLACE_RETRY_ATTEMPTS` times, `_WIN_REPLACE_RETRY_DELAY_SECONDS`
    apart, before giving up; the final attempt's exception (if it still
    fails) propagates to the caller's existing `except Exception` handler,
    which logs and swallows it exactly as before this retry existed.
    """
    if sys.platform != "win32":
        os.replace(src, dst)
        return

    for attempt in range(_WIN_REPLACE_RETRY_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _WIN_REPLACE_RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_WIN_REPLACE_RETRY_DELAY_SECONDS)


def default_status_dir() -> Path:
    """`$XDG_STATE_HOME/muxplex-deck`, default `~/.local/state/muxplex-deck`."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / _STATUS_DIR_NAME


def default_status_path() -> Path:
    return default_status_dir() / _STATUS_FILE_NAME


def build_status(
    *,
    pid: int,
    device_connected: bool,
    device_caps: dict[str, Any] | None,
    server_url: str,
    server_connected: bool,
    last_poll_at: float | None,
    last_error: str | None,
    active_session: str | None,
    active_view: str | None,
    page: int | None,
    hint: str | None = None,
) -> dict[str, Any]:
    """Build the status dict written to disk. Pure -- no I/O, easy to test.

    `device_caps` (when the device is connected) is the same dict shape
    `deck_probe.capabilities.describe_capabilities` returns -- model,
    serial, firmware, key_count, key_rows/cols, dial_count,
    has_touchscreen, is_visual, etc. Never include the federation key or
    any other secret here.

    `hint` (optional, additive -- no `SCHEMA_VERSION` bump needed) is
    actionable guidance for why the device couldn't be opened (see
    `hidhelp.explain_open_failure`), published so `muxplex-deck status`
    can show it instead of a stale/misleading status. Older readers that
    don't know this field simply don't see it (`.get()` is used to read
    it back -- see `cli._format_device_line`).
    """
    device: dict[str, Any] = {"connected": device_connected}
    if device_connected and device_caps is not None:
        device["capabilities"] = device_caps
    if hint is not None:
        device["hint"] = hint

    return {
        "schema_version": SCHEMA_VERSION,
        "pid": pid,
        "updated_at": time.time(),
        "device": device,
        "server": {
            "url": server_url,
            "connected": server_connected,
            "last_poll_at": last_poll_at,
            "last_error": last_error,
        },
        "state": {
            "active_session": active_session,
            "active_view": active_view,
            "page": page,
        },
    }


def write_status(status: dict[str, Any], path: Path | None = None) -> None:
    """Atomically write `status` (a JSON-serializable dict) to `path`.

    Best-effort: any failure (permission denied, disk full, read-only FS)
    is logged at WARNING and swallowed -- callers (the sidecar's poll loop)
    must never crash or stall because they couldn't publish status.
    """
    target = path or default_status_path()
    tmp_path: str | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # mkdir's `mode` argument is masked by umask on most platforms --
        # assert 0700 explicitly rather than trusting mkdir(mode=0o700).
        os.chmod(target.parent, 0o700)
        fd, tmp_path = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(status, f, indent=2)
            _replace_with_windows_retry(tmp_path, target)
            tmp_path = None
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    Path(tmp_path).unlink(missing_ok=True)
    except Exception:
        logger.warning("failed to write status file %s", target, exc_info=True)


def read_status(path: Path | None = None) -> dict[str, Any] | None:
    """Read + parse the status file. Returns None if missing/corrupt/unreadable."""
    target = path or default_status_path()
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


class StatusReporter:
    """Accumulates known sidecar state and writes it out on every update.

    Constructed once per `run()` invocation; `update(**fields)` merges the
    given fields into the last-known state and writes the merged result.
    Every call writes -- there is no debounce/throttle -- because the poll
    loop that drives most calls already runs on its own `poll_interval`
    cadence (default 2s), which is exactly the "refresh periodically so a
    stale file is detectable" behavior wanted here. Writes are cheap (a
    few-hundred-byte JSON file); best-effort failure handling lives in
    `write_status`.
    """

    def __init__(self, server_url: str, path: Path | None = None) -> None:
        self._path = path or default_status_path()
        self._state: dict[str, Any] = {
            "device_connected": False,
            "device_caps": None,
            "server_url": server_url,
            "server_connected": False,
            "last_poll_at": None,
            "last_error": None,
            "active_session": None,
            "active_view": None,
            "page": None,
            "hint": None,
        }

    def update(self, **fields: Any) -> None:
        unknown = set(fields) - set(self._state)
        if unknown:
            raise TypeError(
                f"StatusReporter.update: unknown field(s) {sorted(unknown)}"
            )
        self._state.update(fields)
        status = build_status(pid=os.getpid(), **self._state)
        write_status(status, self._path)
