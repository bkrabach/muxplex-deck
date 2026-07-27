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

`launchctl bootstrap` idempotency: unlike `systemctl start` (idempotent --
starting an already-active unit just succeeds), `launchctl bootstrap` exits
5 ("Input/output error") when the job label is ALREADY loaded. A real user
hit this running `service start` against a service that was already
healthy and running: the code used ``check=True``, so launchd's expected
"already loaded" refusal surfaced as an unhandled `CalledProcessError`
traceback instead of the benign no-op it actually is. `_launchd_bootstrap()`
is the shared, non-raising (`check=False`) helper both `_launchd_install()`
and `_launchd_start()` use; both treat exit 5 as success, and any other
nonzero exit as a genuine failure reported via launchctl's own stderr
(never swallowed) rather than a raw traceback. `_launchd_restart()` also
waits for `bootout` to actually finish (it returns before the job is fully
torn down) so the follow-up `bootstrap` doesn't race it into the same
exit-5 rejection for the wrong reason.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config as config_mod

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SYSTEMD_UNIT_DIR: Path = Path.home() / ".config" / "systemd" / "user"
_SYSTEMD_UNIT_PATH: Path = _SYSTEMD_UNIT_DIR / "muxplex-deck.service"

_LAUNCHD_PLIST_DIR: Path = Path.home() / "Library" / "LaunchAgents"
_LAUNCHD_PLIST_PATH: Path = _LAUNCHD_PLIST_DIR / "com.muxplex-deck.plist"
_LAUNCHD_LABEL: str = "com.muxplex-deck"

# `launchctl bootstrap` exit code when the job label is already loaded. This
# is launchd's way of saying "already running" -- not a failure -- so it is
# handled as a benign no-op everywhere `_launchd_bootstrap()` is used, never
# swallowed silently for any OTHER exit code.
_LAUNCHD_ALREADY_LOADED_EXIT = 5

# `launchctl bootout` returns before the job has necessarily finished
# tearing down, so a `restart` (stop then start) can race it: bootstrap
# fires while the old job is still unloading and gets rejected with the
# same exit-5 "already loaded" the still-loading old job produces. Poll
# `service_is_active()` until the job is actually gone (bounded so a stuck
# teardown can't hang `restart` forever).
_LAUNCHD_BOOTOUT_POLL_INTERVAL_SECONDS = 0.2
_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS = 5.0

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


def _print_guidance_block(message: str) -> None:
    """Print a multi-line guidance message in `print_check`'s ✓/! style.

    First line gets the warn mark + 2-space indent; continuation lines
    get a bare 4-space indent -- the same convention `cli.print_check`
    uses (kept as a small local duplicate here to avoid a circular import
    with `cli.py`).
    """
    for i, line in enumerate(message.splitlines()):
        prefix = f"  {_MARK_WARN} " if i == 0 else "    "
        print(f"{prefix}{line}")


def _warn_if_no_udev_rule() -> None:
    """Print the udev remediation block (non-fatal) if no rule is present.

    Delegates the actual guidance text to `hidhelp.udev_guidance()` -- the
    single home for this string (see WSL_COLD_START_SPEC.md P6). That
    function now returns `None` both when udev isn't actually running
    (P4: never print a command known to fail here) and when this is WSL
    (the reload can report success there while the rule still never
    fires for a usbip-attached device -- see AGENTS.md "U7"; a real WSL
    user followed this exact block and lost ~40 minutes). On a healthy
    non-WSL platform this prints nothing, exactly as before. On WSL,
    show the environment-appropriate guidance instead (the proven
    per-attach `chown`) rather than leaving the user with nothing.
    """
    if udev_rule_exists():
        return
    from . import hidhelp

    guidance = hidhelp.udev_guidance()
    if guidance is not None:
        _print_guidance_block(guidance.message)
        return

    from . import wsl as wsl_mod

    if wsl_mod.detect().is_wsl:
        _print_environment_guidance()


def _print_environment_guidance() -> None:
    """Print any WSL / permission guidance relevant to this environment.

    Read-only: only queries (e.g. `usbipd.exe list`), never mutates --
    `service install` may observe the environment but must never attach a
    device (see WSL_COLD_START_SPEC.md section 6.2). Prints nothing on a
    healthy platform (macOS, or native Linux with udev running) -- this is
    what keeps output unchanged for those users.
    """
    from . import hidhelp

    for guidance in hidhelp.explain_environment():
        print()
        _print_guidance_block(guidance.message)


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


def service_is_installed() -> bool:
    """Is the muxplex-deck unit/plist file present on disk?

    Independent of `service_is_active()` -- a service that is installed but
    crash-looping (or simply stopped) is still installed. `doctor`'s
    `check_service_status` used to conflate "not active" with "not
    installed" and recommend `service install` for a service that was
    already installed and failing; this is the file-existence half of the
    fix (see AGENTS.md for the incident: 1113 restarts, then told to
    "install" a service that was already there).
    """
    if _is_darwin():
        return _LAUNCHD_PLIST_PATH.exists()
    return _SYSTEMD_UNIT_PATH.exists()


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


def _config_ready() -> tuple[bool, str | None]:
    """Return (True, None) if config is loadable, else (False, ConfigError message).

    Calls the exact same `config.load_config()` the installed unit's own
    `ExecStart` (`... run`, no `--config` flag) will call at startup, so
    "ready to install" and "ready to run" can never diverge. A config that
    fails this check would make the service crash immediately once
    started, and `Restart=always` (`KeepAlive` on launchd) would then
    restart it every few seconds forever -- see the `service install`
    crash-loop incident in AGENTS.md (1113 restarts on a fresh machine
    with no config yet).
    """
    try:
        config_mod.load_config(None)
    except config_mod.ConfigError as exc:
        return False, str(exc)
    return True, None


def _print_config_not_ready(config_error: str | None) -> None:
    """Explain why install is stopping short of enabling/starting the service."""
    _print_guidance_block(
        "Not enabling or starting yet -- configuration is incomplete:\n"
        + (config_error or "unknown configuration error")
    )
    print()
    print("  Run muxplex-deck init to create it, then run this command again:")
    print("    muxplex-deck service install")
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

    # Never enable/start a unit whose config we already know will make it
    # crash on launch -- see `_config_ready()`. The unit file above is
    # harmless to have on disk either way; only the "make it run" steps are
    # gated.
    config_ready, config_error = _config_ready()
    if not config_ready:
        _print_config_not_ready(config_error)
        return

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


def _report_systemctl_failure(
    action: str, result: subprocess.CompletedProcess[str]
) -> None:
    """Print systemctl's own diagnostics for a genuine (non-idempotent) failure.

    The most common ordinary-user trigger here is running `start`/`restart`
    before `install` (unit not found) -- a real failure, but one that should
    surface as a clear message, not a raw `CalledProcessError` traceback.
    """
    stderr = (result.stderr or "").strip()
    print(
        f"  ERROR: systemctl {action} failed (exit {result.returncode})",
        file=sys.stderr,
    )
    if stderr:
        print(f"    {stderr}", file=sys.stderr)


def _systemd_start() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "start", "muxplex-deck"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        _step_ok("Started the service")
    else:
        _report_systemctl_failure("start", result)
        sys.exit(1)


def _systemd_stop() -> None:
    subprocess.run(["systemctl", "--user", "stop", "muxplex-deck"], check=False)


def _systemd_restart() -> None:
    # `systemctl restart` is a single atomic transaction (unlike launchd's
    # separate bootout + bootstrap), so it needs no unload-race handling --
    # and it is idempotent whether or not the unit was already running. The
    # ordinary failure mode is the unit not being installed yet.
    result = subprocess.run(
        ["systemctl", "--user", "restart", "muxplex-deck"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        _step_ok("Restarted the service")
    else:
        _report_systemctl_failure("restart", result)
        sys.exit(1)


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


def _launchd_bootstrap() -> subprocess.CompletedProcess[str]:
    """Run `launchctl bootstrap` for the muxplex-deck plist, never raising.

    `check=False` deliberately: `bootstrap` exits
    `_LAUNCHD_ALREADY_LOADED_EXIT` (5, "Input/output error") when the job
    label is already loaded -- launchd's way of saying "already running",
    not a failure. Callers must inspect `returncode` themselves and decide
    what that means for them; this helper only runs the command and hands
    back the raw result.
    """
    uid = os.getuid()
    return subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(_LAUNCHD_PLIST_PATH)],
        capture_output=True,
        text=True,
        check=False,
    )


def _report_launchctl_failure(
    action: str, result: subprocess.CompletedProcess[str]
) -> None:
    """Print launchctl's own diagnostics for a genuine (non-idempotent) failure.

    Only the well-known "already loaded" exit code is treated as benign by
    callers; every other nonzero exit prints launchctl's own stderr (which
    already includes its "re-run as root for richer errors" hint) so the
    real failure is still visible -- just as a clear message instead of an
    unhandled `CalledProcessError` traceback.
    """
    stderr = (result.stderr or "").strip()
    print(
        f"  ERROR: launchctl {action} failed (exit {result.returncode})",
        file=sys.stderr,
    )
    if stderr:
        print(f"    {stderr}", file=sys.stderr)


def _wait_for_launchd_unload(timeout: float | None = None) -> bool:
    """Poll until the launchd job is no longer loaded, or `timeout` elapses.

    `launchctl bootout` returns before the job has necessarily finished
    tearing down -- immediately re-bootstrapping afterward can race it and
    get rejected with the same "already loaded" exit code a genuinely
    still-running job would produce. Returns True once `service_is_active()`
    reports the job gone; returns False if it was still present when the
    timeout elapsed (the caller decides how to proceed -- see
    `_launchd_restart`).

    `timeout` defaults to `_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS` read at CALL time
    (not as a bound default argument) so tests can monkeypatch the module
    constant and have it take effect.
    """
    if timeout is None:
        timeout = _LAUNCHD_BOOTOUT_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    while True:
        if not service_is_active():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LAUNCHD_BOOTOUT_POLL_INTERVAL_SECONDS)


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

    # Never bootstrap (load + start) a job whose config we already know will
    # make it crash on launch -- see `_config_ready()`. `KeepAlive` would
    # then have launchd relaunch it forever. The plist above is harmless to
    # have on disk either way; only the "make it run" step is gated.
    config_ready, config_error = _config_ready()
    if not config_ready:
        _print_config_not_ready(config_error)
        return

    result = _launchd_bootstrap()
    if result.returncode == 0:
        _step_ok("Loaded + started the service (launchctl bootstrap)")
    elif result.returncode == _LAUNCHD_ALREADY_LOADED_EXIT:
        _step_ok(
            "Service was already loaded -- plist rewritten; run "
            "`muxplex-deck service restart` to apply changes"
        )
    else:
        _report_launchctl_failure("bootstrap", result)

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
    result = _launchd_bootstrap()
    if result.returncode == 0:
        _step_ok("Started the service (launchctl bootstrap)")
    elif result.returncode == _LAUNCHD_ALREADY_LOADED_EXIT:
        _step_ok("Service was already running (nothing to start)")
    else:
        _report_launchctl_failure("bootstrap", result)
        sys.exit(1)


def _launchd_stop() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False)


def _launchd_restart() -> None:
    _launchd_stop()
    if not _wait_for_launchd_unload():
        _step_warn(
            f"Service did not fully unload within "
            f"{_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS:.0f}s -- attempting restart anyway"
        )

    result = _launchd_bootstrap()
    if result.returncode == 0:
        _step_ok("Restarted the service (launchctl bootstrap)")
    elif result.returncode == _LAUNCHD_ALREADY_LOADED_EXIT:
        _step_ok("Service is running")
    else:
        _report_launchctl_failure("bootstrap", result)
        sys.exit(1)


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
