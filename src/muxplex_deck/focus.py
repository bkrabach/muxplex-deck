"""Bring the local muxplex PWA to the foreground (platform seam).

One public function, `focus_app(name)`, called from a background thread when
a key press switches the active session (see `main._ActiveRuntime`). It is
strictly best-effort: it never raises, never blocks longer than a short
timeout, and a failure here must never disturb the session switch itself.

Platform dispatch happens here and only here:

- macOS (``darwin``): implemented via ``open -a <name>`` (see
  `_focus_macos` for why that beats ``osascript``).
- Windows (``win32``): implemented via `_focus_windows` -- see that
  function's docstring for what `focus_app` means on this platform (a
  window-title substring, not an app bundle name -- the PWA runs inside a
  generic browser process here, unlike macOS's named `.app`) and the real,
  documented limits of forcing foreground focus from a background process.
- Everything else (Linux/WSL): logs ONE INFO line per process ("no
  foreground-focus support on this platform") and no-ops.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Protocol

logger = logging.getLogger("muxplex_deck")

# `open -a` normally returns in well under a second; if it hangs, give up
# rather than tying up the connect worker thread.
FOCUS_TIMEOUT_SECONDS = 2.0

_unsupported_logged = False


def macos_focus_command(name: str) -> list[str]:
    """The exact argv the macOS path runs to activate `name`.

    ``open -a`` (rather than ``osascript -e 'tell application ... to
    activate'``) because it activates the app if running, launches it if
    not, works for Chrome/Safari-installed PWAs (which are real ``.app``
    bundles under e.g. ``~/Applications/Chrome Apps.localized/``), and
    needs no AppleScript/Automation permission prompt. List-args, never a
    shell, so the app name is passed verbatim.
    """
    return ["open", "-a", name]


def focus_app(name: str) -> None:
    """Bring the named application/window to the foreground, best-effort.

    Never raises. Empty `name` is a pure no-op (feature disabled). On
    Linux/WSL (no foreground-focus implementation exists there), logs one
    INFO line per process and no-ops.
    """
    if not name:
        return

    if sys.platform == "darwin":
        _focus_macos(name)
        return

    if sys.platform == "win32":
        _focus_windows(name)
        return

    global _unsupported_logged
    if not _unsupported_logged:
        logger.info(
            "focus_app=%r is configured, but foreground focus has no "
            "Linux/WSL implementation; ignoring",
            name,
        )
        _unsupported_logged = True


def _focus_macos(name: str) -> None:
    command = macos_focus_command(name)
    logger.debug("focus: running %s", command)
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=FOCUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("focus: could not activate %r: %s", name, exc)
        return
    if result.returncode != 0:
        # Most common cause: the app name doesn't match any installed app
        # ("Unable to find application named ..."). Warn with the real
        # stderr so the user can fix the config, but never raise.
        logger.warning(
            "focus: %r failed (rc=%d): %s",
            " ".join(command),
            result.returncode,
            result.stderr.strip() or "(no stderr)",
        )


# ---------------------------------------------------------------------------
# Windows -- bring a browser-hosted PWA window to the foreground.
#
# macOS's `open -a <name>` has no Windows equivalent: the muxplex PWA runs
# inside a generic browser process there (msedge.exe/chrome.exe under an
# `--app=` URL), not a named, individually-addressable `.app` bundle. What
# CAN identify it reliably is its window TITLE (browsers set the window
# title to the page's <title>) -- so on Windows, `focus_app` is
# reinterpreted as "the substring to look for in a top-level window's
# title" rather than "an app name". The config field keeps its name and
# position (see `config.py`'s updated docstring/validation message); only
# its Windows *meaning* differs. Existing macOS configs are untouched.
#
# Windows also restricts which process may steal the foreground -- a bare
# `SetForegroundWindow()` call from a background process is documented to
# either be refused outright, or (the more common and more confusing
# failure) report success while the OS only flashes the target's taskbar
# icon and never actually raises the window. This is a deliberate
# anti-focus-stealing measure (see
# https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setforegroundwindow),
# not a bug to brute-force around. The one documented, widely-used
# technique that reliably works from a background process is
# `AttachThreadInput`: temporarily fuse this thread's input queue with the
# CURRENT foreground window's thread (which satisfies the "received the
# last input event" exemption Microsoft's own docs describe as one of the
# conditions that allows a foreground switch), call
# `SetForegroundWindow`, then detach. It is still best-effort -- verified
# afterward by comparing `GetForegroundWindow()` to the target, with a
# logged (never raised) notice on a miss, exactly as the module docstring
# promises for `focus_app` as a whole.
# ---------------------------------------------------------------------------

_SW_RESTORE = 9


class _Win32Api(Protocol):
    """The narrow slice of user32/kernel32 `_focus_windows` needs.

    Exists so tests can supply a fake without touching real
    `ctypes.windll`/`ctypes.WINFUNCTYPE` -- both are genuinely ABSENT as
    attributes on non-Windows Python builds (monkeypatching `sys.platform`
    to `"win32"` does not make them exist; only a real Windows process
    has them). Production code only ever constructs `_RealWin32Api` from
    inside `_focus_windows`, on a real `win32` process -- tests never
    reach that constructor.
    """

    def list_window_titles(self) -> list[tuple[int, str]]:
        """(hwnd, title) for every visible top-level window."""
        ...

    def is_iconic(self, hwnd: int) -> bool: ...

    def restore(self, hwnd: int) -> None: ...

    def get_foreground_window(self) -> int: ...

    def get_window_thread_id(self, hwnd: int) -> int: ...

    def get_current_thread_id(self) -> int: ...

    def attach_thread_input(
        self, this_id: int, other_id: int, attach: bool
    ) -> bool: ...

    def set_foreground_window(self, hwnd: int) -> bool: ...


class _RealWin32Api:
    """ctypes-backed `_Win32Api`. Constructed only on a real `win32` process.

    `ctypes.windll` and `ctypes.WINFUNCTYPE` are Windows-only attributes
    of the `ctypes` module (absent -- not merely non-functional -- on any
    other platform), so both are looked up lazily here, inside
    `__init__`/methods, never at module import time.
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        self._kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    def list_window_titles(self) -> list[tuple[int, str]]:
        ctypes = self._ctypes
        wintypes = self._wintypes
        user32 = self._user32
        results: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]
        def _callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value:
                results.append((hwnd, buf.value))
            return True

        user32.EnumWindows(_callback, 0)
        return results

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def restore(self, hwnd: int) -> None:
        self._user32.ShowWindow(hwnd, _SW_RESTORE)

    def get_foreground_window(self) -> int:
        return self._user32.GetForegroundWindow()

    def get_window_thread_id(self, hwnd: int) -> int:
        return self._user32.GetWindowThreadProcessId(hwnd, None)

    def get_current_thread_id(self) -> int:
        return self._kernel32.GetCurrentThreadId()

    def attach_thread_input(self, this_id: int, other_id: int, attach: bool) -> bool:
        return bool(self._user32.AttachThreadInput(this_id, other_id, attach))

    def set_foreground_window(self, hwnd: int) -> bool:
        return bool(self._user32.SetForegroundWindow(hwnd))


def _real_win32_api() -> _Win32Api:
    return _RealWin32Api()


def _find_window(api: _Win32Api, name: str) -> int | None:
    """First visible top-level window whose title contains `name` (case-insensitive)."""
    needle = name.lower()
    for hwnd, title in api.list_window_titles():
        if needle in title.lower():
            return hwnd
    return None


def _raise_to_foreground(api: _Win32Api, hwnd: int) -> bool:
    """Best-effort foreground steal via the `AttachThreadInput` technique.

    Returns True only if `GetForegroundWindow()` actually reports `hwnd`
    afterward -- see the module section docstring above: a bare
    `SetForegroundWindow` return value is NOT proof the switch took,
    Windows can report success while only flashing the taskbar button.
    """
    if api.is_iconic(hwnd):
        api.restore(hwnd)

    current_thread = api.get_current_thread_id()
    fg_hwnd = api.get_foreground_window()
    fg_thread = api.get_window_thread_id(fg_hwnd) if fg_hwnd else 0

    attached = False
    if fg_thread and fg_thread != current_thread:
        attached = api.attach_thread_input(current_thread, fg_thread, True)

    try:
        api.set_foreground_window(hwnd)
    finally:
        if attached:
            api.attach_thread_input(current_thread, fg_thread, False)

    return api.get_foreground_window() == hwnd


def _focus_windows(name: str) -> None:
    api = _real_win32_api()
    hwnd = _find_window(api, name)
    if hwnd is None:
        logger.warning(
            "focus: no window found with title containing %r -- is the "
            "muxplex PWA open in a browser window?",
            name,
        )
        return
    try:
        confirmed = _raise_to_foreground(api, hwnd)
    except OSError as exc:
        logger.warning("focus: could not activate window %r: %s", name, exc)
        return
    if not confirmed:
        logger.info(
            "focus: requested foreground for %r, but Windows did not "
            "confirm the switch (likely flashed the taskbar icon instead "
            "-- this is a documented OS restriction on background "
            "processes stealing focus, not a muxplex-deck bug)",
            name,
        )
