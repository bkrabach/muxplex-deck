"""Bring the local muxplex PWA to the foreground (platform seam).

One public function, `focus_app(name)`, called from a background thread when
a key press switches the active session (see `main._ActiveRuntime`). It is
strictly best-effort: it never raises, never blocks longer than a short
timeout, and a failure here must never disturb the session switch itself.

Platform dispatch happens here and only here:

- macOS (``darwin``): implemented via ``open -a <name>`` (see
  `_focus_macos` for why that beats ``osascript``).
- Everything else: logs ONE INFO line per process ("macOS-only for now")
  and no-ops, keeping a clean seam for a Windows implementation later.
"""

from __future__ import annotations

import logging
import subprocess
import sys

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
    """Bring the named application to the foreground, best-effort.

    Never raises. Empty `name` is a pure no-op (feature disabled). On
    non-macOS platforms, logs one INFO line per process and no-ops.
    """
    if not name:
        return

    if sys.platform == "darwin":
        _focus_macos(name)
        return

    # TODO(windows): implement for the user's Windows box -- likely via
    # pywin32 SetForegroundWindow or an `explorer.exe`/PowerShell shim.
    global _unsupported_logged
    if not _unsupported_logged:
        logger.info(
            "focus_app=%r is configured, but foreground focus is macOS-only "
            "for now; ignoring",
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
