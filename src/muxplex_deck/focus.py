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

import ctypes
import logging
import subprocess
import sys
from ctypes import wintypes
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
# Windows also restricts which process may steal the foreground. Per
# Microsoft's own documentation
# (https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-setforegroundwindow),
# `SetForegroundWindow` only succeeds if the calling process is itself the
# foreground process, was started by it, there is currently no foreground
# window, the calling process received the last input event, the
# foreground lock timeout has expired, or either process is being debugged
# -- otherwise Windows silently downgrades the call to a taskbar-icon
# flash. A windowless background process like this sidecar's connect
# thread satisfies NONE of those by default, which is a deliberate
# anti-focus-stealing measure, not a bug to brute-force around.
#
# Two DOCUMENTED techniques are combined here, in order, real-hardware-
# VERIFIED as insufficient individually (2026-07-28: `AttachThreadInput`
# alone requested the switch but Windows only flashed the taskbar icon):
#
# 1. `SendInput` synthesizes a lone ALT keydown/keyup. This is not a
#    brute-force hack -- Microsoft's own `LockSetForegroundWindow` Remarks
#    state plainly: "The system automatically enables calls to
#    SetForegroundWindow if the user presses the ALT key or takes some
#    action that causes the system itself to change the foreground
#    window" (learn.microsoft.com/windows/win32/api/winuser/
#    nf-winuser-locksetforegroundwindow). `SendInput` is documented as the
#    way to synthesize that keypress without a real keyboard
#    (learn.microsoft.com/windows/win32/api/winuser/nf-winuser-sendinput).
#    This is almost certainly what `AttachThreadInput` alone was missing:
#    a background process with no window and no prior input has nothing
#    to "attach" that satisfies the exemption on its own -- see
#    stackoverflow.com/questions/19136365 ("doesn't work if your app is a
#    background process without any windows and input focus"). No window,
#    no message pump, and (critically, per the user's own requirement) NO
#    system-wide setting change -- the effect is scoped to this one call,
#    unlike `SystemParametersInfo(SPI_SETFOREGROUNDLOCKTIMEOUT, 0)`, which
#    was considered and REJECTED: that call persists a registry value
#    (`HKCU\Control Panel\Desktop\ForegroundLockTimeout`) affecting every
#    application on the machine, not just this one, and was never made
#    opt-in -- unacceptable for a background sidecar to do silently.
# 2. `AttachThreadInput` (kept, harmless, still may help): temporarily fuse
#    this thread's input queue with the CURRENT foreground window's thread
#    (an independent way of satisfying the "received the last input
#    event" exemption), call `SetForegroundWindow`, then detach.
#
# The combination is still best-effort -- verified afterward by comparing
# `GetForegroundWindow()` to the target, with a logged (never raised)
# notice on a miss, exactly as the module docstring promises for
# `focus_app` as a whole. UNVERIFIED on real Windows hardware whether the
# `SendInput` step closes the gap the 2026-07-28 report found --
# `AttachThreadInput` alone was proven insufficient there; the analysis
# above is Microsoft-documentation-grounded, not yet hardware-confirmed.
# ---------------------------------------------------------------------------

_SW_RESTORE = 9

# SendInput's ALT-tap technique (see the module section comment above).
# ctypes.wintypes is a plain-data module (DWORD/WORD/LONG are just
# c_ulong/c_ushort/c_long aliases) importable and usable on ANY platform --
# unlike `ctypes.windll`, it needs no real Windows process, so these
# structures are safe to define unconditionally at module import time.
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_VK_MENU = 0x12  # ALT


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    )


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUnion(ctypes.Union):
    # Real Windows `INPUT` unions mi/ki/hi -- MOUSEINPUT is the largest
    # member, so the union (and therefore `ctypes.sizeof(_INPUT)`) must
    # include all three or `SendInput`'s `cbSize` check rejects the call.
    _fields_ = (
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    )


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUnion))


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

    def tap_alt_key(self) -> None:
        """Synthesize a lone ALT keydown+keyup via `SendInput`.

        See the module section comment above `_SW_RESTORE` for why this
        exists and what documented Windows behavior it relies on.
        """
        ...


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

    def tap_alt_key(self) -> None:
        inputs = (_INPUT * 2)()
        inputs[0].type = _INPUT_KEYBOARD
        inputs[0].union.ki = _KEYBDINPUT(
            wVk=_VK_MENU, wScan=0, dwFlags=0, time=0, dwExtraInfo=None
        )
        inputs[1].type = _INPUT_KEYBOARD
        inputs[1].union.ki = _KEYBDINPUT(
            wVk=_VK_MENU, wScan=0, dwFlags=_KEYEVENTF_KEYUP, time=0, dwExtraInfo=None
        )
        self._user32.SendInput(
            2, self._ctypes.byref(inputs), self._ctypes.sizeof(_INPUT)
        )


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
    """Best-effort foreground steal, combining two documented techniques.

    Returns True only if `GetForegroundWindow()` actually reports `hwnd`
    afterward -- see the module section comment above: a bare
    `SetForegroundWindow` return value is NOT proof the switch took,
    Windows can report success while only flashing the taskbar button.

    Order matters: `tap_alt_key()` runs FIRST, unconditionally -- it is
    Microsoft's own documented mechanism for re-enabling
    `SetForegroundWindow` system-wide (see the module section comment),
    and real hardware showed `AttachThreadInput` alone is not sufficient
    for a windowless background process. `AttachThreadInput` is kept
    afterward as a second, independent way of satisfying the "received
    the last input event" exemption -- harmless if the ALT tap already
    cleared the block, and still worth attempting if it didn't.
    """
    if api.is_iconic(hwnd):
        api.restore(hwnd)

    api.tap_alt_key()

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
