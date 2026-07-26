"""muxplex_deck/service.py -- System service management (systemd on Linux, launchd on macOS).

Ported near 1:1 from muxplex's own `service.py` (see that repo's
`muxplex/service.py`), with two sidecar-specific differences:

1. ``Restart=always`` (not muxplex's ``on-failure``) + a ``loginctl
   enable-linger`` attempt on install -- this is a headless, always-on
   sidecar meant to survive logout, not a service a user interactively
   restarts.
2. A udev-rule check on Linux install: by default a non-root user cannot
   open the Stream Deck's HID device (this is why the sidecar is normally
   run via ``sudo``), so `service_install()` warns loudly with a
   copy-pasteable remediation block when no matching rule exists, rather
   than silently installing a service that will fail to open the device.

`service_install()`/`service_uninstall()` narrate every step they take
(unit/plist path, enable/start, resulting status) using the same ✓/!
2-space-indent style as `cli.doctor()` -- a silent success path left a real
user unsure whether `service install` had done anything at all.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYSTEMD_UNIT_DIR: Path = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_UNIT_PATH: Path = _SYSTEMD_UNIT_DIR / "muxplex-deck.service"

_LAUNCHD_PLIST_DIR: Path = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST_PATH: Path = _LAUNCHD_PLIST_DIR / "com.muxplex-deck.plist"
_LAUNCHD_LABEL: str = "com.muxplex-deck"

# Elgato Stream Deck USB vendor id -- used to detect an existing udev rule.
_ELGATO_VENDOR_ID = "0fd9"
_UDEV_RULE_DIRS: tuple[Path, ...] = (
    Path("/etc/udev/rules.d"),
    Path("/usr/lib/udev/rules.d"),
)

_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=muxplex-deck
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5s
TimeoutStopSec=10
KillMode=mixed
Environment=PATH={safe_path}

[Install]
WantedBy=default.target
"""

_LAUNCHD_PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_arguments_xml}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{safe_path}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/muxplex-deck.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/muxplex-deck.err</string>
</dict>
</plist>
"""

_UDEV_RULE_CONTENT = (
    'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0660", TAG+="uaccess"\n'
    'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", MODE="0660", TAG+="uaccess"\n'
)

_UDEV_REMEDIATION = f"""\
  ! No udev rule found for the Stream Deck (vendor id {_ELGATO_VENDOR_ID}).
    Without it, the service (running as your user, not root) will fail to
    open the device. Install a rule once:

      sudo tee /etc/udev/rules.d/70-streamdeck.rules >/dev/null <<'EOF'
{_UDEV_RULE_CONTENT}EOF
      sudo udevadm control --reload-rules && sudo udevadm trigger

    Then unplug and replug the Stream Deck (or re-run `usbipd attach` under WSL).
"""

# Same ✓/! 2-space-indent style as `cli.doctor()`'s `print_check` -- kept as
# a small local duplicate (rather than importing from `cli.py`) to avoid a
# circular import: `cli.py` already imports from this module at call time.
_MARK_OK = "\033[32m\u2713\033[0m"
_MARK_WARN = "\033[33m!\033[0m"


def _step_ok(message: str) -> None:
    print(f"  {_MARK_OK} {message}")


def _step_warn(message: str) -> None:
    print(f"  {_MARK_WARN} {message}")


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def _is_darwin() -> bool:
    """Return True if running on macOS."""
    return sys.platform == "darwin"


def _have_systemctl() -> bool:
    """Return True if systemctl is on PATH (gates all systemd service operations)."""
    return shutil.which("systemctl") is not None


def _resolve_muxplex_deck_bin() -> str:
    """Return the muxplex-deck binary path (joined string -- systemd splits it).

    Prefers the ``muxplex-deck`` executable on PATH; falls back to
    ``<sys.executable> -m muxplex_deck`` when not found.
    """
    which = shutil.which("muxplex-deck")
    if which:
        return which
    return f"{sys.executable} -m muxplex_deck"


def _resolve_bin_for_launchd() -> list[str]:
    """Return the argv token list for the muxplex-deck binary in a launchd plist.

    Prefers ``~/.local/bin/muxplex-deck`` (stable uv-tool console-script
    symlink that survives ``uv tool reinstall``). Falls back to
    ``shutil.which("muxplex-deck")``, then to ``[sys.executable, "-m",
    "muxplex_deck"]`` as explicitly split tokens.

    Each element must become its own ``<string>`` in ProgramArguments --
    launchd does **not** shell-split inside a ``<string>``; an element like
    ``"python3 -m muxplex_deck"`` is treated as a literal executable name,
    causing the daemon to silently fail to start.
    """
    local_bin = Path.home() / ".local" / "bin" / "muxplex-deck"
    if local_bin.exists() and os.access(str(local_bin), os.X_OK):
        return [str(local_bin)]

    which = shutil.which("muxplex-deck")
    if which:
        return [which]

    return [sys.executable, "-m", "muxplex_deck"]


# ---------------------------------------------------------------------------
# udev rule detection (Linux HID-permission caveat)
# ---------------------------------------------------------------------------


def udev_rule_exists() -> bool:
    """Return True if any udev rule file mentions the Elgato vendor id.

    Scans `/etc/udev/rules.d/` and `/usr/lib/udev/rules.d/` for a `*.rules`
    file whose contents mention vendor id `0fd9` (case-insensitive). Never
    writes to `/etc` itself -- only reports.
    """
    for rules_dir in _UDEV_RULE_DIRS:
        if not rules_dir.is_dir():
            continue
        for rule_file in rules_dir.glob("*.rules"):
            try:
                text = rule_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if _ELGATO_VENDOR_ID in text.lower():
                return True
    return False


def _warn_if_no_udev_rule() -> None:
    """Print the udev remediation block (non-fatal) if no rule is present."""
    if not udev_rule_exists():
        print(_UDEV_REMEDIATION)


def service_is_active() -> bool:
    """Best-effort: is the muxplex-deck service currently active/running?

    Public (moved here from `cli._service_is_active`) so both `cli.update()`
    and `cli.check_hid_openable()` share one implementation -- the latter
    needs it to distinguish "our own service holds the device" (expected,
    not a failure) from a genuine HID-permission problem. Never raises:
    a missing service manager or a not-installed service both read as
    "not active", which is the correct doctor/status answer either way.
    """
    if _is_darwin():
        uid = os.getuid()
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{_LAUNCHD_LABEL}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return False
        return result.returncode == 0

    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "muxplex-deck"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _enable_linger() -> None:
    """Best-effort `loginctl enable-linger` so the service survives logout.

    muxplex has no analog to this -- it's a normal user-triggered server, not
    a headless always-on sidecar. Failure (no loginctl, no systemd-logind,
    permission denied) is reported but never fatal to install.
    """
    if shutil.which("loginctl") is None:
        print("  ! loginctl not found -- skipping enable-linger (service may")
        print("    stop when you log out; install systemd-logind or enable")
        print("    lingering manually if this is a headless always-on box)")
        return
    user = getpass.getuser()
    result = subprocess.run(
        ["loginctl", "enable-linger", user],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print(f"  Linger enabled for {user} (service survives logout)")
    else:
        print(f"  ! Could not enable linger for {user}: {result.stderr.strip()}")
        print("    The service may stop when you log out of this session.")


# ---------------------------------------------------------------------------
# Private implementations -- systemd (Linux)
# ---------------------------------------------------------------------------


def _print_next_steps() -> None:
    print()
    print("  Next:")
    print("    muxplex-deck status        -- see connected hardware + connection state")
    print("    muxplex-deck service logs  -- tail live logs")
    print()


def _systemd_install() -> None:
    print("\nmuxplex-deck service install (systemd --user)\n")

    bin_path = _resolve_muxplex_deck_bin()
    safe_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    exec_start = f"{bin_path} run"
    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(
        exec_start=exec_start, safe_path=safe_path
    )
    _SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_UNIT_PATH.write_text(unit_content)
    _step_ok(f"Wrote unit file: {_SYSTEMD_UNIT_PATH}")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    _step_ok("Reloaded the systemd user daemon")

    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "muxplex-deck"], check=True
    )
    _step_ok("Enabled + started the service")

    _enable_linger()
    _warn_if_no_udev_rule()

    if service_is_active():
        _step_ok("Service is running")
    else:
        _step_warn(
            "Service was started but is not reporting active -- check: "
            "muxplex-deck service logs"
        )

    _print_next_steps()


def _systemd_uninstall() -> None:
    print("\nmuxplex-deck service uninstall (systemd --user)\n")

    result = subprocess.run(
        ["systemctl", "--user", "stop", "muxplex-deck"], check=False
    )
    if result.returncode == 0:
        _step_ok("Stopped the service")
    else:
        _step_warn("Service was not running (nothing to stop)")

    subprocess.run(["systemctl", "--user", "disable", "muxplex-deck"], check=False)
    _step_ok("Disabled the service")

    had_unit = _SYSTEMD_UNIT_PATH.exists()
    _SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
    _step_ok(
        f"Removed unit file: {_SYSTEMD_UNIT_PATH}"
        if had_unit
        else "Unit file already absent"
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    _step_ok("Reloaded the systemd user daemon")
    print()


def _systemd_start() -> None:
    subprocess.run(["systemctl", "--user", "start", "muxplex-deck"], check=True)


def _systemd_stop() -> None:
    subprocess.run(["systemctl", "--user", "stop", "muxplex-deck"], check=False)


def _systemd_restart() -> None:
    subprocess.run(["systemctl", "--user", "restart", "muxplex-deck"], check=True)


def _systemd_status() -> None:
    subprocess.run(
        ["systemctl", "--user", "status", "muxplex-deck", "--no-pager"], check=False
    )


def _systemd_logs() -> None:
    try:
        subprocess.run(
            ["journalctl", "--user", "-u", "muxplex-deck", "-f"], check=False
        )
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Private implementations -- launchd (macOS)
# ---------------------------------------------------------------------------


def _launchd_install() -> None:
    print("\nmuxplex-deck service install (launchd)\n")

    bin_args = _resolve_bin_for_launchd()
    argv = [*bin_args, "run"]
    # Each argv token is its own <string> element. launchd does NOT
    # shell-split inside a <string>, so we must NOT put the whole command
    # (e.g. "python3 -m muxplex_deck") into a single element.
    program_arguments_xml = "\n".join(f"        <string>{arg}</string>" for arg in argv)
    base_path = os.environ.get("PATH", "/usr/bin:/bin")
    safe_path = f"/opt/homebrew/bin:/usr/local/bin:{base_path}"
    plist_content = _LAUNCHD_PLIST_TEMPLATE.format(
        label=_LAUNCHD_LABEL,
        program_arguments_xml=program_arguments_xml,
        safe_path=safe_path,
    )
    _LAUNCHD_PLIST_DIR.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PLIST_PATH.write_text(plist_content)
    _step_ok(f"Wrote plist: {_LAUNCHD_PLIST_PATH}")

    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_LAUNCHD_PLIST_PATH)], check=True
    )
    _step_ok("Loaded + started the service (launchctl bootstrap)")

    if service_is_active():
        _step_ok("Service is running")
    else:
        _step_warn(
            "Service was started but is not reporting active -- check: "
            "muxplex-deck service logs"
        )

    _print_next_steps()


def _launchd_uninstall() -> None:
    print("\nmuxplex-deck service uninstall (launchd)\n")

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False
    )
    if result.returncode == 0:
        _step_ok("Stopped + unloaded the service")
    else:
        _step_warn("Service was not loaded (nothing to unload)")

    had_plist = _LAUNCHD_PLIST_PATH.exists()
    _LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
    _step_ok(
        f"Removed plist: {_LAUNCHD_PLIST_PATH}"
        if had_plist
        else "Plist file already absent"
    )
    print()


def _launchd_start() -> None:
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_LAUNCHD_PLIST_PATH)], check=True
    )


def _launchd_stop() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False)


def _launchd_restart() -> None:
    _launchd_stop()
    _launchd_start()


def _launchd_status() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "print", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False)


def _launchd_logs() -> None:
    try:
        subprocess.run(["tail", "-f", "/tmp/muxplex-deck.log"], check=False)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Public API -- platform-dispatching wrappers
# ---------------------------------------------------------------------------


def _unsupported_platform_error(command: str) -> None:
    """Print a clear error when neither launchd nor systemd is available."""
    print(
        f"  ERROR: 'muxplex-deck service {command}' requires systemd (Linux) or "
        "launchd (macOS), neither of which was found.",
        file=sys.stderr,
    )
    print(
        "  Run muxplex-deck directly to start the sidecar without a service manager:",
        file=sys.stderr,
    )
    print("    muxplex-deck run", file=sys.stderr)


def service_install() -> None:
    """Install the muxplex-deck service unit for the current user."""
    if _is_darwin():
        _launchd_install()
    elif _have_systemctl():
        _systemd_install()
    else:
        _unsupported_platform_error("install")


def service_uninstall() -> None:
    """Remove the muxplex-deck service unit for the current user."""
    if _is_darwin():
        _launchd_uninstall()
    elif _have_systemctl():
        _systemd_uninstall()
    else:
        _unsupported_platform_error("uninstall")


def service_start() -> None:
    """Start the muxplex-deck service."""
    if _is_darwin():
        _launchd_start()
    elif _have_systemctl():
        _systemd_start()
    else:
        _unsupported_platform_error("start")


def service_stop() -> None:
    """Stop the muxplex-deck service."""
    if _is_darwin():
        _launchd_stop()
    elif _have_systemctl():
        _systemd_stop()
    else:
        _unsupported_platform_error("stop")


def service_restart() -> None:
    """Restart the muxplex-deck service."""
    if _is_darwin():
        _launchd_restart()
    elif _have_systemctl():
        _systemd_restart()
    else:
        _unsupported_platform_error("restart")


def service_status() -> None:
    """Print the current status of the muxplex-deck service."""
    if _is_darwin():
        _launchd_status()
    elif _have_systemctl():
        _systemd_status()
    else:
        _unsupported_platform_error("status")


def service_logs() -> None:
    """Stream or print logs for the muxplex-deck service."""
    if _is_darwin():
        _launchd_logs()
    elif _have_systemctl():
        _systemd_logs()
    else:
        _unsupported_platform_error("logs")
