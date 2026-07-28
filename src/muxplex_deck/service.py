"""muxplex_deck/service.py -- System service management (systemd on Linux,
launchd on macOS, Task Scheduler on Windows).

Ported near 1:1 from muxplex's own `service.py` (see that repo's
`muxplex/service.py`), with sidecar-specific differences:

1. ``Restart=always`` (not muxplex's ``on-failure``) + a ``loginctl
   enable-linger`` attempt on install -- this is a headless, always-on
   sidecar meant to survive logout, not a service a user interactively
   restarts.
2. A udev-rule check on Linux install: by default a non-root user cannot
   open the Stream Deck's HID device (this is why the sidecar is normally
   run via ``sudo``), so `service_install()` warns loudly with a
   copy-pasteable remediation block when no matching rule exists, rather
   than silently installing a service that will fail to open the device.
3. Windows has no analog to a background *service* at all -- see
   WINDOWS_NATIVE_SPEC.md section 1 for why a real Windows Service is
   disqualified (admin-only registration, LocalSystem's wrong `%USERPROFILE%`
   or a stored password for a named account) and why an at-logon Task
   Scheduler task in the interactive user's own context is used instead.

``service_install()``/``service_uninstall()``/``service_start()``/
``service_restart()``/``service_stop()`` all narrate what they did through
`report.py`'s VERDICT/STATE/ACTION renderer -- the same one
`cli.doctor()`/`cli.status()` use (v0.7.0). This module used to print
step-by-step progress with a local 2-space-indent style as each subprocess
call completed; it now collects a `report.Check` per step and renders ONE
report at the end, for the same reason `doctor`/`status` do: one coherent,
paste-friendly report beats a scroll of print statements, and it is the
only way a Windows implementation with nothing resembling
``systemctl status``'s own formatting can present itself consistently with
the other two platforms. `service_logs()` is unchanged in kind: a raw
passthrough stream (`journalctl -f` / `tail -f` / Windows'
`Get-Content -Wait`) -- and remains one. `service_stop()` is NO LONGER
silent, as it used to be (v0.9.3 and earlier): each platform's stop
function now also attempts a best-effort deck-screen clear once the
sidecar is CONFIRMED stopped (never before -- see `_reset_deck_best_effort()`
and each `_*_stop()`'s own docstring), and reports whether that succeeded.
Real-world report: `muxplex-deck service stop` (Windows) left the Stream
Deck's LCD keys showing the last-painted session icons indefinitely,
because `schtasks /End` (`TerminateProcess`) bypasses the Python
interpreter entirely -- no signal handler, no `finally`, no
`main._shutdown_cleanup()` ever runs. The same bypass-Python risk exists
on systemd/launchd too, just less often: both escalate to `SIGKILL` if the
sidecar doesn't exit within their own stop timeout (`TimeoutStopSec`/
`ExitTimeOut`), so the reset is applied uniformly on all three platforms,
not just Windows. `service_status()`
keeps macOS/Linux's raw passthrough to `launchctl print` / `systemctl status`
(deliberately -- their own output carries more detail than we could
reconstruct, and it is display-only, never parsed for a decision); Windows'
`service_status()` has no analogous rich external command to shell out to
(`schtasks /Query /V` is explicitly rejected -- see WINDOWS_NATIVE_SPEC.md
section 1.6 -- because its output is localized and verbose) so it renders
its own report natively.

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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

# `systemctl restart` / `launchctl bootstrap` both return once the new
# process has been LAUNCHED, not once it has done anything -- the new
# sidecar needs at least one poll cycle to open the device (or notice it
# can't) and publish its first status write via `statusfile.StatusReporter`.
# Until then, `status.json` still holds the PREVIOUS process's last
# snapshot, which `muxplex-deck status` would otherwise present as current,
# healthy truth (the restart-race incident in AGENTS.md: 2 false failures
# read from a dying process's stale-but-recent write, moments before it
# exited). `restart` polls for a status write from the NEW process (by pid,
# via `service_main_pid()`) before reporting success, bounded so a genuinely
# slow-starting sidecar can't hang the command forever.
#
# Windows-specific timing: `_win_task_query()` shells out to `powershell.exe`
# (~200-400ms per spawn, versus a direct `systemctl`/`launchctl` call), so
# the same 0.2s/5s budget on POSIX would poll too aggressively and time out
# too early on Windows. WINDOWS_NATIVE_SPEC.md section 1.4 recommends 0.5s/
# 10s (20 polls) there instead. Set once at import time from `sys.platform`;
# `_wait_for_fresh_status()` re-reads these module globals at call/loop time
# (not as bound default arguments), so tests can still monkeypatch them
# directly regardless of which platform default was picked at import.
if sys.platform == "win32":
    _RESTART_STATUS_POLL_INTERVAL_SECONDS = 0.5
    _RESTART_STATUS_TIMEOUT_SECONDS = 10.0
else:
    _RESTART_STATUS_POLL_INTERVAL_SECONDS = 0.2
    _RESTART_STATUS_TIMEOUT_SECONDS = 5.0

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

# Windows Task Scheduler task name, root folder ("\\muxplex-deck"). Kept as
# a module-level variable (not a plain constant) so tests can monkeypatch it
# to a per-test-unique value -- see conftest.py's Rail 2 extension -- and so
# a real developer machine's own registered task is never at risk from a
# test that forgets to mock `subprocess.run` (Rail 4 is the primary guard;
# this is belt-and-suspenders for the identifier itself).
_WIN_TASK_NAME = "muxplex-deck"

# Schedule.Service COM API's TASK_STATE_RUNNING enum value (verified against
# the Task Scheduler Schema documentation -- NOT executed on real hardware,
# see this change's real-hardware sign-off checklist).
_WIN_TASK_STATE_RUNNING = 4

_WIN_QUERY_SUBPROCESS_TIMEOUT_SECONDS = 10.0

# How long `_win_restart()` waits for the OLD task instance to actually stop
# reporting TASK_STATE_RUNNING before issuing `/Run` for the new one -- see
# `_win_wait_for_task_stopped()`'s docstring for the real-hardware incident
# this closes. Same interval/timeout shape as launchd's own unload wait
# (`_LAUNCHD_BOOTOUT_*`); Windows gets its own constants rather than reusing
# those so each platform's timing can be tuned independently.
_WIN_STOP_POLL_INTERVAL_SECONDS = 0.2
_WIN_STOP_POLL_TIMEOUT_SECONDS = 5.0

# One PowerShell/COM query answers `is_installed`/`is_active`/`main_pid` at
# once -- see WINDOWS_NATIVE_SPEC.md section 1.4 for why this must be COM
# (`Schedule.Service`) and never `schtasks /Query` text: `schtasks`' console
# output is LOCALIZED ("Status:"/"Running" are translated on non-English
# Windows), so parsing it for a correctness-critical predicate would silently
# break on any non-English machine. `.format()`-escaped literal braces
# (`{{`/`}}`) are PowerShell script-block delimiters, not template fields.
_WIN_TASK_QUERY_SCRIPT_TEMPLATE = (
    "$s = New-Object -ComObject Schedule.Service; $s.Connect(); "
    "try {{ $t = $s.GetFolder('\\').GetTask('{task_name}') }} "
    "catch {{ 'MISSING'; exit }} "
    "$p = 0; foreach ($i in $t.GetInstances(0)) {{ $p = $i.EnginePID }} "
    "'OK ' + $t.State + ' ' + $p"
)

# WINDOWS_NATIVE_SPEC.md section 1.2 -- every setting here is load-bearing,
# not decorative:
#   - LogonTrigger + UserId: no stored password, `~` resolves to the real
#     user's profile (not LocalSystem's wrong one).
#   - Repetition PT1M + MultipleInstancesPolicy IgnoreNew: ONE restart
#     mechanism that covers every death mode (hang, hard kill, clean exit),
#     unlike `<RestartOnFailure>` which only fires on a nonzero exit.
#   - ExecutionTimeLimit PT0S: the default is 3 DAYS, after which Task
#     Scheduler kills the task outright -- a sidecar that silently dies
#     after 72h is worse than one that never started.
#   - DisallowStartIfOnBatteries=false / StopIfGoingOnBatteries=false: both
#     default to true, which would refuse to start (or kill mid-session) on
#     a laptop running on battery.
#   - LogonType InteractiveToken + RunLevel LeastPrivilege: registration in
#     one's own context needs no admin, and this never elevates.
_WIN_TASK_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>muxplex-deck -- drives an Elgato Stream Deck against a muxplex server</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user_id}</UserId>
      <Repetition>
        <Interval>PT1M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user_id}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{pythonw_path}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def _is_darwin() -> bool:
    """Return True if running on macOS."""
    return sys.platform == "darwin"


def _is_windows() -> bool:
    """Return True if running on native Windows."""
    return sys.platform == "win32"


def _have_systemctl() -> bool:
    """Return True if systemctl is on PATH (gates all systemd service operations)."""
    return shutil.which("systemctl") is not None


def service_manager_available() -> bool:
    """Whether ANY supported service manager exists on this machine.

    True on macOS (launchd), native Windows (Task Scheduler, at-logon --
    see WINDOWS_NATIVE_SPEC.md section 1), and Linux with systemd; False on
    any Linux without `systemctl` (containers, minimal distros).

    Callers that are about to OFFER `service install` -- not merely
    dispatch to it -- must check this first and skip the offer entirely
    when it's False, rather than asking a yes/no question and only then
    hitting `_unsupported_platform_error()`. That "offer, then fail" shape
    is the same class of bug as the v0.5.2 crash-loop incident: presenting
    an action that provably cannot succeed on this machine (see AGENTS.md).
    """
    return _is_darwin() or _is_windows() or _have_systemctl()


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


def _udev_install_check() -> Any | None:
    """Build the install report's "udev" `Check`, or `None` if there's nothing to say.

    Replaces the old `_warn_if_no_udev_rule()` (which printed directly) --
    same branching, but returns a `report.Check` for the caller to fold
    into the single end-of-command report instead of printing mid-flight.

    `None` when a rule already exists, and (matching the previous
    behavior exactly) when udev isn't live and this isn't WSL: the U-DEAD
    guidance -- if any -- still comes from `explain_environment()` calls
    elsewhere (e.g. `doctor()`), never duplicated here. `hidhelp.udev_guidance()`
    itself returns `None` both when udev isn't actually running (never
    print a command known to fail here) and on WSL (the reload can report
    success there while the rule still never fires for a usbip-attached
    device -- see AGENTS.md "U7"); on WSL this falls back to the proven
    per-attach `chown` guidance from `explain_environment()` instead of the
    raw udev block.
    """
    from . import report

    if udev_rule_exists():
        return None
    from . import hidhelp

    guidance = hidhelp.udev_guidance()
    if guidance is not None:
        return report.Check("udev", report.ACT, guidance.message)

    from . import wsl as wsl_mod

    if wsl_mod.detect().is_wsl:
        env_guidances = hidhelp.explain_environment()
        if env_guidances:
            combined = "\n".join(g.message for g in env_guidances)
            return report.Check("udev", report.ACT, combined)

    return None


def _enable_linger() -> Any:
    """Best-effort `loginctl enable-linger` so the service survives logout.

    Returns a `report.Check` for the install report instead of printing
    directly (see the module docstring on the report-based narration
    rewrite). muxplex has no analog to this -- it's a normal
    user-triggered server, not a headless always-on sidecar. Failure (no
    loginctl, no systemd-logind, permission denied) is reported but never
    fatal to install.
    """
    from . import report

    if shutil.which("loginctl") is None:
        return report.Check(
            "linger",
            report.ACT,
            "loginctl not found -- skipping enable-linger (service may stop "
            "when you log out; install systemd-logind or enable lingering "
            "manually if this is a headless always-on box)",
        )
    user = getpass.getuser()
    result = subprocess.run(
        ["loginctl", "enable-linger", user],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return report.Check(
            "linger",
            report.FINE,
            f"Linger enabled for {user} (service survives logout)",
        )
    return report.Check(
        "linger",
        report.ACT,
        f"Could not enable linger for {user}: {result.stderr.strip()} -- "
        "the service may stop when you log out of this session.",
    )


# ---------------------------------------------------------------------------
# Report-based narration -- shared by systemd/launchd/Windows install,
# uninstall, start, and restart. See the module docstring for why this
# replaced the old print-as-you-go `_step_ok`/`_step_warn` style.
# ---------------------------------------------------------------------------


def _failure_check(
    tool: str, action: str, result: subprocess.CompletedProcess[str]
) -> Any:
    """Build an ACT `Check` from a genuine (non-idempotent) command failure.

    The underlying tool's own stderr is folded into the Check's value
    (which may be multi-line -- `report.format_check_line` wraps/hangs
    multi-line values correctly) so the real diagnostic stays visible,
    exactly as it was when this printed straight to stderr.
    """
    from . import report

    stderr = (result.stderr or "").strip()
    value = f"{tool} {action} failed (exit {result.returncode})"
    if stderr:
        value = f"{value}\n{stderr}"
    return report.Check("service", report.ACT, value)


def _restart_result_check(
    success_message: str, *, fresh_status_check: Callable[[], bool] | None = None
) -> Any:
    """Build the final restart `Check`: success only once fresh status is

    actually observed, otherwise an honest "still waiting" warning. Shared
    by systemd/launchd/Windows so all three platforms make the same
    promise: never claim a step (the new process being up and reporting)
    that was not actually verified -- see AGENTS.md's restart-race
    incident, which this directly closes.

    `fresh_status_check` overrides the default `_wait_for_fresh_status()`
    call. systemd/launchd never pass it (byte-for-byte unchanged
    behavior); `_win_restart()` passes `_win_wait_for_fresh_status` bound
    to its pre-restart baseline pid, because Windows cannot use a live-pid
    comparison at all -- see `service_main_pid()`'s and
    `_win_wait_for_fresh_status()`'s docstrings for why.
    """
    from . import report

    check = fresh_status_check or _wait_for_fresh_status
    if check():
        return report.Check("service", report.FINE, success_message)
    return report.Check(
        "service",
        report.ACT,
        "Restart command sent, but the service has not published fresh "
        f"status within {_RESTART_STATUS_TIMEOUT_SECONDS:.0f}s -- it may "
        "still be starting. run: muxplex-deck status",
    )


def _service_decision(check: Any) -> Any:
    """Build the ACTION decision for a service report's first act-now check."""
    from . import report

    if check.subject == "config":
        return report.Decision(
            commands=["muxplex-deck init"],
            prose="Creates the config so the service can start without crash-looping.",
        )
    command = report.extract_run_command(check.value)
    if command:
        return report.Decision(commands=[command])
    return report.Decision(commands=[], prose=check.value)


def _build_service_action(collapsed: list[Any]) -> list[str] | None:
    from . import report

    act_items = [c for c in collapsed if c.glyph == report.ACT]
    if not act_items:
        return None
    overflow = None
    if len(act_items) > 1:
        overflow = f"{len(act_items) - 1} more after this -- rerun the command."
    action = report.Action(
        decision=_service_decision(act_items[0]), overflow_note=overflow
    )
    return report.render_action(action)


def _render_service_report(items: list[Any]) -> None:
    """Render ONE VERDICT/STATE/ACTION report for a service command and print it.

    Shared by install/uninstall/start/restart on all three platforms --
    see the module docstring. Reuses `report.verdict_readiness()` (the same
    "Ready."/"Not ready -- N things to do." phrasing `doctor()` uses)
    rather than inventing a bespoke verdict vocabulary per verb.
    """
    from . import report

    collapsed = report.collapsed_checks(items)
    action_count = report.count_actions([c.glyph for c in collapsed])
    verdict = report.verdict_readiness(action_count)
    state_lines = report.render_items(items, show_all=False, utf8=report.utf8_capable())
    action_lines = _build_service_action(collapsed)
    sys.stdout.write(report.render(verdict, state_lines, action_lines))


# ---------------------------------------------------------------------------
# Deck-screen reset after a CONFIRMED stop -- shared by all three platforms.
# ---------------------------------------------------------------------------


def _reset_deck_best_effort() -> Any:
    """Best-effort: open the (now-free) Stream Deck from THIS process and
    reset it, clearing whatever the sidecar had painted on its LCD keys.

    Real-world report: `muxplex-deck service stop` (Windows) stopped the
    scheduled task, but the deck's LCD keys kept showing the last-painted
    session icons indefinitely. Root cause -- see `main._shutdown_cleanup()`'s
    own docstring: a hard kill (Windows `schtasks /End`'s `TerminateProcess`,
    or a `SIGKILL` systemd/launchd escalate to after their own stop-timeout
    elapses) bypasses the Python interpreter entirely -- no signal handler,
    no `finally`, no `_shutdown_cleanup()` ever runs. The only way to blank
    the screen after a hard kill is for a DIFFERENT process to open the
    device afterward and reset it, so this function exists to be that
    different process.

    MUST only be called once the sidecar is CONFIRMED no longer running --
    see each `_*_stop()`'s own docstring for how confirmation is
    established on that platform (reusing the same unload/stopped-poll each
    platform's `restart` already relies on). Calling this while the sidecar
    might still be alive would race it for the exclusive HID handle.

    Best-effort and non-fatal, exactly like `main._safe_close`: a missing
    or unplugged deck, a deck still held by something else, or a hidapi
    load failure are all reported here (via the returned `Check`, so the
    caller can tell whether the screen was actually cleared) but this
    function itself never raises -- a failed reset must never turn a
    successful `service stop` into a reported failure, since the service is
    stopped either way, screen or no screen.

    Reuses `main._safe_close()` (reset() + close(), with its own
    TransportError/exception swallowing) rather than re-implementing that
    error handling a second time here -- one reset semantics, not two, per
    `_shutdown_cleanup`'s own docstring. Both imports are per-call (not
    module-level) so tests can patch `device_real.RealDeviceManager` the
    same way `main._build_manager`/`cli.py`'s hardware checks already rely
    on (see `tests/conftest.py`'s `_neutralize_real_hid` rail).
    """
    from . import report

    try:
        from .device_real import RealDeviceManager

        manager = RealDeviceManager()
    except Exception as exc:  # noqa: BLE001 -- e.g. DeviceProbeError (hidapi missing)
        return report.Check(
            "deck",
            report.ACT,
            f"could not access the Stream Deck to clear its screen: {exc}",
        )

    try:
        deck = manager.find_device()
    except Exception as exc:  # noqa: BLE001 -- enumeration failure, never fatal
        return report.Check(
            "deck", report.ACT, f"could not look for a Stream Deck to clear: {exc}"
        )

    if deck is None:
        return report.Check(
            "deck", report.FINE, "No Stream Deck detected -- nothing to clear"
        )

    try:
        deck.open()
    except Exception as exc:  # noqa: BLE001 -- unplugged/claimed mid-race, never fatal
        return report.Check(
            "deck",
            report.ACT,
            f"Stream Deck detected but could not be opened to clear its screen: {exc}",
        )

    from .main import _safe_close

    _safe_close(deck)  # never raises -- reset() + close(), errors swallowed inside
    return report.Check("deck", report.FINE, "Cleared the Stream Deck screen")


# ---------------------------------------------------------------------------
# Private implementations -- systemd (Linux)
# ---------------------------------------------------------------------------


def _config_ready() -> tuple[bool, str | None]:
    """Return (True, None) if config is loadable, else (False, ConfigError message).

    Calls the exact same `config.load_config()` the installed unit's own
    `ExecStart` (`... run`, no `--config` flag) will call at startup, so
    "ready to install" and "ready to run" can never diverge. A config that
    fails this check would make the service crash immediately once
    started, and `Restart=always` (`KeepAlive` on launchd, the `IgnoreNew`
    repetition trigger on Windows) would then restart it every few seconds
    forever -- see the `service install` crash-loop incident in AGENTS.md
    (1113 restarts on a fresh machine with no config yet).
    """
    try:
        config_mod.load_config(None)
    except config_mod.ConfigError as exc:
        return False, str(exc)
    return True, None


def _systemd_install() -> None:
    from . import report

    items: list[Any] = []

    bin_path = _resolve_muxplex_deck_bin()
    safe_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    exec_start = f"{bin_path} run"
    unit_content = _SYSTEMD_UNIT_TEMPLATE.format(
        exec_start=exec_start, safe_path=safe_path
    )
    _SYSTEMD_UNIT_DIR.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_UNIT_PATH.write_text(unit_content)
    items.append(
        report.Check("unit", report.FINE, f"Wrote unit file: {_SYSTEMD_UNIT_PATH}")
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    items.append(
        report.Check("daemon", report.FINE, "Reloaded the systemd user daemon")
    )

    # Never enable/start a unit whose config we already know will make it
    # crash on launch -- see `_config_ready()`. The unit file above is
    # harmless to have on disk either way; only the "make it run" steps are
    # gated.
    config_ready, config_error = _config_ready()
    if not config_ready:
        items.append(
            report.Check(
                "config", report.ACT, config_error or "unknown configuration error"
            )
        )
        _render_service_report(items)
        return

    subprocess.run(
        ["systemctl", "--user", "enable", "--now", "muxplex-deck"], check=True
    )
    items.append(report.Check("enable", report.FINE, "Enabled + started the service"))

    items.append(_enable_linger())
    udev_item = _udev_install_check()
    if udev_item is not None:
        items.append(udev_item)

    if service_is_active():
        items.append(report.Check("service", report.FINE, "Service is running"))
    else:
        items.append(
            report.Check(
                "service",
                report.ACT,
                "Service was started but is not reporting active -- run: "
                "muxplex-deck service logs",
            )
        )

    _render_service_report(items)


def _systemd_uninstall() -> None:
    from . import report

    items: list[Any] = []

    result = subprocess.run(
        ["systemctl", "--user", "stop", "muxplex-deck"], check=False
    )
    items.append(
        report.Check(
            "stop",
            report.FINE,
            "Stopped the service"
            if result.returncode == 0
            else "Service was not running (nothing to stop)",
        )
    )

    subprocess.run(["systemctl", "--user", "disable", "muxplex-deck"], check=False)
    items.append(report.Check("disable", report.FINE, "Disabled the service"))

    had_unit = _SYSTEMD_UNIT_PATH.exists()
    _SYSTEMD_UNIT_PATH.unlink(missing_ok=True)
    items.append(
        report.Check(
            "unit",
            report.FINE,
            f"Removed unit file: {_SYSTEMD_UNIT_PATH}"
            if had_unit
            else "Unit file already absent",
        )
    )

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    items.append(
        report.Check("daemon", report.FINE, "Reloaded the systemd user daemon")
    )

    _render_service_report(items)


def _systemd_start() -> None:
    result = subprocess.run(
        ["systemctl", "--user", "start", "muxplex-deck"],
        capture_output=True,
        text=True,
        check=False,
    )
    from . import report

    if result.returncode == 0:
        _render_service_report(
            [report.Check("service", report.FINE, "Started the service")]
        )
    else:
        _render_service_report([_failure_check("systemctl", "start", result)])
        sys.exit(1)


def _systemd_stop() -> None:
    """Stop the service, then best-effort clear the deck's screen.

    `systemctl --user stop` blocks until the unit is confirmed stopped --
    systemd itself escalates to `SIGKILL` if the sidecar doesn't exit
    within `TimeoutStopSec` (see the unit template's `KillMode=mixed`), so
    by the time this call returns the sidecar is genuinely gone, one way
    or the other. `service_is_active()` is still checked explicitly before
    attempting the reset (cheap, and mirrors launchd/Windows' own explicit
    confirmation below) rather than trusting the blocking call alone.

    This is NOT a Windows-only concern: a `SIGKILL` escalation bypasses
    Python exactly like Windows' `TerminateProcess` does (`main.
    _shutdown_cleanup()` never runs), just less often, since a sidecar
    that notices `shutting_down` promptly exits on its own SIGTERM first.
    See `_reset_deck_best_effort()`'s docstring for the full reasoning.
    """
    from . import report

    subprocess.run(["systemctl", "--user", "stop", "muxplex-deck"], check=False)

    items: list[Any] = []
    if not service_is_active():
        items.append(_reset_deck_best_effort())
    else:
        items.append(
            report.Check(
                "deck",
                report.ACT,
                "Could not confirm the service fully stopped -- skipping screen clear",
            )
        )
    _render_service_report(items)


def _wait_for_fresh_status(timeout: float | None = None) -> bool:
    """Poll until `status.json`'s recorded pid matches the service's live
    MainPID, or `timeout` elapses.

    See `_RESTART_STATUS_TIMEOUT_SECONDS`'s docstring for why this exists:
    the process a restart just launched needs at least one poll cycle
    before it publishes its own status, and comparing pids (not wall-clock
    age) is the only reliable way to tell "this snapshot belongs to the
    process running right now" from "this is the previous process's last
    write" -- the exact restart-race incident in AGENTS.md.

    Returns True once a matching status is observed; False if `timeout`
    elapses first. Never raises -- `service_main_pid()` and
    `statusfile.read_status()` are both best-effort/never-raise themselves.
    """
    from . import statusfile

    if timeout is None:
        timeout = _RESTART_STATUS_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    while True:
        current_pid = service_main_pid()
        if current_pid is not None:
            data = statusfile.read_status()
            if data is not None and data.get("pid") == current_pid:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_RESTART_STATUS_POLL_INTERVAL_SECONDS)


def _win_wait_for_fresh_status(
    baseline_pid: int | None, timeout: float | None = None
) -> bool:
    """Windows analogue of `_wait_for_fresh_status()` -- a baseline-pid
    diff, not a live-pid match.

    `service_main_pid()` always returns `None` on Windows: Task
    Scheduler's `EnginePID` names its own engine-HOST process, never the
    sidecar it launched -- VERIFIED on real hardware, see that function's
    docstring. A live-pid comparison can therefore never succeed on this
    platform; reusing `_wait_for_fresh_status()` unmodified would make
    every Windows restart report "has not published fresh status" after
    the FULL timeout, even a perfectly healthy one -- a regression of the
    exact contract this function exists to protect, just from the
    opposite direction (always-fail instead of always-pass).

    Instead, this compares against `baseline_pid`: the status file's
    recorded pid from BEFORE the restart began (`_win_restart()` reads it
    prior to calling `_win_stop()`). `_win_stop()` hard-kills the previous
    process before `_win_start()` launches a new one, so any pid the new
    process reports afterward is necessarily a NEW process's write, never
    the terminated one's -- the sidecar's own self-reported pid remains
    the authoritative signal (nothing here is fabricated), this is just a
    different, equally reliable way of reading freshness from it.
    `baseline_pid=None` (no status existed before this restart, e.g. the
    very first ever start) means ANY freshly observed pid counts.

    Same contract as `_wait_for_fresh_status()`: never raises, True once a
    fresh write is observed, False if `timeout` elapses first.
    """
    from . import statusfile

    if timeout is None:
        timeout = _RESTART_STATUS_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    while True:
        data = statusfile.read_status()
        recorded_pid = data.get("pid") if data is not None else None
        if recorded_pid is not None and recorded_pid != baseline_pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_RESTART_STATUS_POLL_INTERVAL_SECONDS)


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
    if result.returncode != 0:
        _render_service_report([_failure_check("systemctl", "restart", result)])
        sys.exit(1)

    _render_service_report([_restart_result_check("Restarted the service")])


def _systemd_status() -> None:
    # Raw passthrough of systemctl's own (richer) status output --
    # deliberately NOT converted to the report renderer. See the module
    # docstring: this is display-only, never parsed for a decision, and
    # carries detail (enabled/loaded state, recent journal lines) we could
    # not reconstruct without re-parsing it ourselves.
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
    from . import report

    items: list[Any] = []

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
    items.append(
        report.Check("plist", report.FINE, f"Wrote plist: {_LAUNCHD_PLIST_PATH}")
    )

    # Never bootstrap (load + start) a job whose config we already know will
    # make it crash on launch -- see `_config_ready()`. `KeepAlive` would
    # then have launchd relaunch it forever. The plist above is harmless to
    # have on disk either way; only the "make it run" step is gated.
    config_ready, config_error = _config_ready()
    if not config_ready:
        items.append(
            report.Check(
                "config", report.ACT, config_error or "unknown configuration error"
            )
        )
        _render_service_report(items)
        return

    result = _launchd_bootstrap()
    if result.returncode == 0:
        items.append(
            report.Check(
                "service",
                report.FINE,
                "Loaded + started the service (launchctl bootstrap)",
            )
        )
    elif result.returncode == _LAUNCHD_ALREADY_LOADED_EXIT:
        items.append(
            report.Check(
                "service",
                report.FINE,
                "Was already loaded -- plist rewritten; run: muxplex-deck service restart",
            )
        )
    else:
        items.append(_failure_check("launchctl", "bootstrap", result))

    if service_is_active():
        items.append(report.Check("running", report.FINE, "Service is running"))
    else:
        items.append(
            report.Check(
                "running",
                report.ACT,
                "Service was started but is not reporting active -- run: "
                "muxplex-deck service logs",
            )
        )

    _render_service_report(items)


def _launchd_uninstall() -> None:
    from . import report

    items: list[Any] = []

    uid = os.getuid()
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False
    )
    items.append(
        report.Check(
            "stop",
            report.FINE,
            "Stopped + unloaded the service"
            if result.returncode == 0
            else "Service was not loaded (nothing to unload)",
        )
    )

    had_plist = _LAUNCHD_PLIST_PATH.exists()
    _LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
    items.append(
        report.Check(
            "plist",
            report.FINE,
            f"Removed plist: {_LAUNCHD_PLIST_PATH}"
            if had_plist
            else "Plist file already absent",
        )
    )

    _render_service_report(items)


def _launchd_start() -> None:
    from . import report

    result = _launchd_bootstrap()
    if result.returncode == 0:
        _render_service_report(
            [
                report.Check(
                    "service", report.FINE, "Started the service (launchctl bootstrap)"
                )
            ]
        )
    elif result.returncode == _LAUNCHD_ALREADY_LOADED_EXIT:
        _render_service_report(
            [
                report.Check(
                    "service", report.FINE, "Was already running (nothing to start)"
                )
            ]
        )
    else:
        _render_service_report([_failure_check("launchctl", "bootstrap", result)])
        sys.exit(1)


def _launchd_stop() -> None:
    """Stop the service, then best-effort clear the deck's screen.

    `launchctl bootout` returns before the job has necessarily finished
    tearing down (see `_wait_for_launchd_unload()`'s docstring -- the exact
    race `_launchd_restart()` already guards against), so this polls for
    the job to actually disappear before attempting the reset below --
    never race a still-shutting-down sidecar for the exclusive HID handle.
    If launchd itself eventually escalates to `SIGKILL` (its default
    `ExitTimeOut`), that bypasses Python exactly like Windows'
    `TerminateProcess` does; see `_reset_deck_best_effort()`'s docstring.
    """
    from . import report

    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False)

    items: list[Any] = []
    if _wait_for_launchd_unload():
        items.append(_reset_deck_best_effort())
    else:
        items.append(
            report.Check(
                "deck",
                report.ACT,
                "Could not confirm the service fully stopped within "
                f"{_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS:.0f}s -- skipping screen clear",
            )
        )
    _render_service_report(items)


def _launchd_restart() -> None:
    from . import report

    items: list[Any] = []

    _launchd_stop()
    if not _wait_for_launchd_unload():
        items.append(
            report.Check(
                "unload",
                report.ACT,
                f"Service did not fully unload within {_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS:.0f}s "
                "-- attempting restart anyway",
            )
        )

    result = _launchd_bootstrap()
    if result.returncode == 0:
        success_message = "Restarted the service (launchctl bootstrap)"
    elif result.returncode == _LAUNCHD_ALREADY_LOADED_EXIT:
        success_message = "Service is running"
    else:
        items.append(_failure_check("launchctl", "bootstrap", result))
        _render_service_report(items)
        sys.exit(1)

    items.append(_restart_result_check(success_message))
    _render_service_report(items)


def _launchd_status() -> None:
    # Raw passthrough of launchctl's own status output -- see
    # `_systemd_status()`'s comment; the same reasoning applies here.
    uid = os.getuid()
    subprocess.run(["launchctl", "print", f"gui/{uid}/{_LAUNCHD_LABEL}"], check=False)


def _launchd_logs() -> None:
    try:
        subprocess.run(["tail", "-f", "/tmp/muxplex-deck.log"], check=False)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Private implementations -- Task Scheduler (native Windows)
#
# See WINDOWS_NATIVE_SPEC.md section 1 for the full design rationale. In
# short: a real Windows Service is disqualified before HID access is even
# considered (S1: creating/deleting one needs admin; S2: it runs as
# LocalSystem, whose %USERPROFILE% is the wrong home for ~/.config and the
# federation key, or as a named account whose password would have to be
# stored in the SCM; S3: Python needs pywin32's pythonservice.exe or a
# third-party wrapper). A scheduled task registered in the CURRENT USER's
# own context via `schtasks /Create /XML`, triggered at logon, needs no
# admin and no stored password.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WinTaskInfo:
    """One COM query's answer to all three service predicates at once."""

    exists: bool
    state: int | None
    pid: int | None


def _parse_win_task_query(stdout: str) -> WinTaskInfo:
    """Parse `_win_task_query()`'s PowerShell output. Pure -- never raises.

    Matches `service_main_pid()`'s existing never-raise contract: any
    unparseable output (garbage, empty, a truncated line) reads as "cannot
    determine" -- the conservative default of `exists=False` -- rather than
    guessing. Handles, without raising: "MISSING", "OK 4 12345", "OK 3 0"
    (state parses, pid 0 means no running instance -> None), garbage, and
    an empty string.
    """
    text = (stdout or "").strip()
    if not text.startswith("OK"):
        return WinTaskInfo(exists=False, state=None, pid=None)
    parts = text.split()
    if len(parts) < 3:
        return WinTaskInfo(exists=True, state=None, pid=None)
    try:
        state = int(parts[1])
    except ValueError:
        state = None
    try:
        pid_raw = int(parts[2])
    except ValueError:
        pid_raw = 0
    return WinTaskInfo(exists=True, state=state, pid=pid_raw if pid_raw > 0 else None)


def _win_task_query() -> WinTaskInfo:
    """Query Task Scheduler via COM for existence/state/pid, all in one spawn.

    Never raises: a missing `powershell.exe` or a subprocess timeout both
    read as "not installed" (the same conservative default `_parse_win_task_query`
    uses for unparseable output), never a false positive.

    `stdin=subprocess.DEVNULL`: this and every other Windows subprocess
    call below closes stdin explicitly rather than inheriting ours -- see
    `_win_install()`'s docstring for the real incident (a `schtasks`
    password prompt) this defends against generally, not just there.
    """
    script = _WIN_TASK_QUERY_SCRIPT_TEMPLATE.format(task_name=_WIN_TASK_NAME)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_WIN_QUERY_SUBPROCESS_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return WinTaskInfo(exists=False, state=None, pid=None)
    return _parse_win_task_query(result.stdout)


def _resolve_pythonw() -> str:
    """Resolve `pythonw.exe` next to `sys.executable`; fall back to `sys.executable`.

    WINDOWS_NATIVE_SPEC.md section 1.2: `pythonw.exe` is the GUI-subsystem
    interpreter (no console window) -- required, because the task runs in
    the logged-on user's own interactive session and a console-subsystem
    binary (the `muxplex-deck.exe` shim, or plain `sys.executable`) would
    show a console window sitting on the desktop for the sidecar's whole
    life. UNVERIFIED whether `pythonw.exe` actually exists inside a `uv
    tool install` venv on Windows -- see this change's real-hardware
    sign-off checklist, item 1. Never fails install over this: falls back
    to `sys.executable` and lets the caller warn instead.
    """
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return sys.executable


def _win_user_id() -> str:
    """Best-effort `DOMAIN\\username` (or bare username) for the task's UserId."""
    domain = os.environ.get("USERDOMAIN")
    name = os.environ.get("USERNAME") or getpass.getuser()
    return f"{domain}\\{name}" if domain else name


def _win_default_log_path() -> Path:
    """`<status_dir>/muxplex-deck.log` -- one state directory, not two.

    `pythonw.exe` leaves `sys.stdout`/`sys.stderr` as `None`
    (WINDOWS_NATIVE_SPEC.md section 1.5), so the task's `--log-file`
    argument must point somewhere; alongside `status.json` keeps every bit
    of this sidecar's runtime state in one place.
    """
    from . import statusfile

    return statusfile.default_status_dir() / "muxplex-deck.log"


def _win_task_xml_path() -> Path:
    """Where the generated Task Scheduler XML is written, for `schtasks /Create /XML`."""
    from . import statusfile

    return statusfile.default_status_dir() / "muxplex-deck-task.xml"


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _win_task_arguments(log_file: Path) -> str:
    """The task action's `<Arguments>` -- no `cmd.exe` wrapper, no shell redirection.

    WINDOWS_NATIVE_SPEC.md section 1.2 constraint 2: a wrapper would break
    the PID contract (`EnginePID` would report the wrapper's pid, not the
    sidecar's) and show a console window. Logging goes to a file from
    inside Python instead of via shell redirection -- see `--log-file`.
    """
    return f'-m muxplex_deck run --log-file "{log_file}"'


def _win_task_xml(*, pythonw_path: str, log_file: Path, user_id: str) -> str:
    arguments = _win_task_arguments(log_file)
    return _WIN_TASK_XML_TEMPLATE.format(
        user_id=_xml_escape(user_id),
        pythonw_path=_xml_escape(pythonw_path),
        arguments=_xml_escape(arguments),
    )


def _write_win_task_xml(path: Path, content: str) -> None:
    """Write Task Scheduler XML as UTF-16 LE with a BOM.

    WINDOWS_NATIVE_SPEC.md section 10.3: multiple independent reports say
    `schtasks /Create /XML` requires the file to be UTF-16 LE with a BOM
    (`FF FE`) or it is rejected as malformed; others report plain UTF-8
    working. This is the ONE place this repo's blanket `encoding="utf-8"`
    convention (IMPLEMENTATION_PHILOSOPHY.md) is deliberately violated, and
    it is UNVERIFIED on real hardware -- see this change's real-hardware
    sign-off checklist, item 3. Writing "utf-16-le" plus an explicit BOM
    character makes the byte order deterministic regardless of the host's
    native byte order, rather than relying on Python's "utf-16" codec's
    native-endian default.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\ufeff" + content, encoding="utf-16-le")


def _win_install() -> None:
    from . import report

    items: list[Any] = []

    pythonw = _resolve_pythonw()
    if not pythonw.lower().endswith("pythonw.exe"):
        items.append(
            report.Check(
                "pythonw",
                report.ACT,
                f"pythonw.exe not found next to this Python install -- falling "
                f"back to {pythonw}, which shows a console window for the "
                "sidecar's whole life. Cosmetic only -- the task still works.",
            )
        )

    log_file = _win_default_log_path()
    user_id = _win_user_id()
    xml_content = _win_task_xml(
        pythonw_path=pythonw, log_file=log_file, user_id=user_id
    )
    xml_path = _win_task_xml_path()
    _write_win_task_xml(xml_path, xml_content)
    items.append(report.Check("task", report.FINE, f"Wrote task XML: {xml_path}"))

    # Never register a task whose config we already know will make it
    # crash-loop -- see `_config_ready()`. The XML above is harmless to
    # have on disk either way; only actually registering it is gated.
    config_ready, config_error = _config_ready()
    if not config_ready:
        items.append(
            report.Check(
                "config", report.ACT, config_error or "unknown configuration error"
            )
        )
        _render_service_report(items)
        return

    # No `/RU <user>` here -- VERIFIED HANG on real hardware (2026-07):
    # `schtasks /Create /RU <user>` prompts INTERACTIVELY for a password on
    # stdin unless `/RP` is also given, no matter the logon type or whether
    # the user matches who is already logged on -- confirmed by Microsoft's
    # own documentation ("Schtasks always prompts for a password unless you
    # provide one, even when you schedule a task on the local computer
    # using the current user account. This is normal behavior for
    # schtasks." --
    # learn.microsoft.com/windows-server/administration/windows-commands/schtasks-create).
    # `subprocess.run` inherits our stdin by default, so a real terminal
    # blocked silently until the user pressed Enter (submitting a blank
    # password) -- exactly the reported "hangs with no output until I hit
    # Enter" symptom. The XML already fully specifies the identity
    # (`<Principals><Principal><UserId>` + `<LogonType>InteractiveToken`,
    # which needs no password at all), and `/XML` can be used alone when
    # the file already contains that information -- so the fix is to never
    # pass `/RU` on the command line, not to guess an `/RP` value.
    # `stdin=subprocess.DEVNULL` on every Windows subprocess call below is
    # defense-in-depth: if some future schtasks/powershell invocation ever
    # tries to read from stdin for any other reason, it fails fast and
    # loud instead of hanging silently on whatever we happen to inherit.
    create_result = subprocess.run(
        [
            "schtasks",
            "/Create",
            "/TN",
            _WIN_TASK_NAME,
            "/XML",
            str(xml_path),
            "/F",
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if create_result.returncode != 0:
        items.append(_failure_check("schtasks", "/Create", create_result))
        _render_service_report(items)
        sys.exit(1)

    run_result = subprocess.run(
        ["schtasks", "/Run", "/TN", _WIN_TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if run_result.returncode != 0:
        items.append(_failure_check("schtasks", "/Run", run_result))
    elif service_is_active():
        items.append(
            report.Check("service", report.FINE, "Registered + started the task")
        )
    else:
        items.append(
            report.Check(
                "service",
                report.ACT,
                "Task was started but is not reporting active -- run: "
                "muxplex-deck service logs",
            )
        )

    # Honest trade-offs, surfaced where the user will see them (never
    # buried) -- WINDOWS_NATIVE_SPEC.md section 1.7.
    items.append(
        report.Check(
            "starts",
            report.FINE,
            "Starts at logon, not boot; worst-case restart latency ~60s vs systemd's ~5s",
        )
    )

    _render_service_report(items)


def _win_uninstall() -> None:
    from . import report

    items: list[Any] = []

    end_result = subprocess.run(
        ["schtasks", "/End", "/TN", _WIN_TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    items.append(
        report.Check(
            "stop",
            report.FINE,
            "Stopped the task" if end_result.returncode == 0 else "Was not running",
        )
    )

    delete_result = subprocess.run(
        ["schtasks", "/Delete", "/TN", _WIN_TASK_NAME, "/F"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    items.append(
        report.Check(
            "delete",
            report.FINE,
            "Removed the scheduled task"
            if delete_result.returncode == 0
            else "Was not registered",
        )
    )

    xml_path = _win_task_xml_path()
    had_xml = xml_path.exists()
    xml_path.unlink(missing_ok=True)
    items.append(
        report.Check(
            "xml",
            report.FINE,
            f"Removed task XML: {xml_path}" if had_xml else "Already absent",
        )
    )

    _render_service_report(items)


def _win_start() -> None:
    from . import report

    result = subprocess.run(
        ["schtasks", "/Run", "/TN", _WIN_TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode == 0:
        _render_service_report(
            [report.Check("service", report.FINE, "Started the task")]
        )
    else:
        _render_service_report([_failure_check("schtasks", "/Run", result)])
        sys.exit(1)


def _win_stop() -> None:
    """Stop the task, then best-effort clear the deck's screen.

    `schtasks /End` is a hard stop (`TerminateProcess`); Windows delivers
    no SIGTERM, so `main._shutdown_cleanup()` never runs and the deck's
    LCD keys keep showing their last-painted frame indefinitely (real-
    world report -- see AGENTS.md and `_reset_deck_best_effort()`'s
    docstring). `/End` itself does not wait for Task Scheduler's own "is
    this task running" bookkeeping to catch up with the killed process
    (the exact lag `_win_wait_for_task_stopped()`'s docstring documents,
    proven by the restart-race incident), so this polls that same helper
    to confirm the task is genuinely no longer running before attempting
    the reset below -- never race a not-yet-dead process for the device.
    """
    from . import report

    subprocess.run(
        ["schtasks", "/End", "/TN", _WIN_TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )

    items: list[Any] = []
    if _win_wait_for_task_stopped():
        items.append(_reset_deck_best_effort())
    else:
        items.append(
            report.Check(
                "deck",
                report.ACT,
                "Could not confirm the task fully stopped within "
                f"{_WIN_STOP_POLL_TIMEOUT_SECONDS:.0f}s -- skipping screen clear",
            )
        )
    _render_service_report(items)


def _win_wait_for_task_stopped(timeout: float | None = None) -> bool:
    """Poll until Task Scheduler no longer reports the task as running.

    **The actual cause of the "restart leaves the task not running" bug,
    VERIFIED on real hardware (2026-07-28):** `schtasks /End` (invoked by
    `_win_stop()`) requests termination but does not synchronously wait for
    Task Scheduler's own internal "is this task currently running"
    bookkeeping to catch up with the killed process. The previous
    `_win_restart()` issued `/Run` immediately after `/End` on the (wrong)
    assumption that "Task Scheduler's /End has no separate unload race to
    wait out" -- a real machine proved that assumption false: `/Run`
    immediately after `/End` left the task in state 3 (`TASK_STATE_READY`,
    not running) instead of actually restarting it, because
    `MultipleInstancesPolicy=IgnoreNew` (WINDOWS_NATIVE_SPEC.md section 1.2,
    chosen deliberately) saw the OLD instance as still "running" at the
    exact instant `/Run` was issued and silently discarded the new run
    request -- net effect: the old process dies, the new one never starts,
    and the user has to `service start` manually.

    This is the exact class of race `_wait_for_launchd_unload()` exists to
    close for launchd's `bootout` (which "returns before the job has
    necessarily finished tearing down"), and WINDOWS_NATIVE_SPEC.md section
    1.6 already specified it for `restart` ("poll `state != RUNNING`
    (bounded, reusing `_wait_for_launchd_unload`'s shape)") -- but the
    original Windows implementation never actually did this. Fixed here.

    Deliberately queries `_win_task_query()` directly rather than going
    through the cross-platform `service_is_active()` dispatcher: this
    function is already Windows-specific (like `_win_wait_for_fresh_status`
    below), and calling the dispatcher would route through `_is_windows()`
    /`_is_darwin()`/`_have_systemctl()` platform probes that have nothing to
    do with what this function checks -- an unnecessary indirection for a
    function that only ever runs from `_win_restart()`.

    Returns True once the task's state is no longer
    `_WIN_TASK_STATE_RUNNING` (or the task cannot be queried at all, which
    reads as "not running" -- the same conservative default
    `_parse_win_task_query()` uses elsewhere); False if `timeout` elapses
    first. The caller (`_win_restart()`) decides how to proceed on a
    timeout -- attempt the restart anyway and report the situation
    honestly, matching `_wait_for_launchd_unload()`'s own contract of never
    raising and never fabricating success.
    """
    if timeout is None:
        timeout = _WIN_STOP_POLL_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    while True:
        info = _win_task_query()
        if info.state != _WIN_TASK_STATE_RUNNING:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_WIN_STOP_POLL_INTERVAL_SECONDS)


def _win_restart() -> None:
    from . import report, statusfile

    items: list[Any] = []

    # Freshness baseline for `_win_wait_for_fresh_status()` below -- MUST be
    # read before `_win_stop()`, so it reflects the process about to be
    # replaced, not the new one. See `service_main_pid()`'s docstring for
    # why Windows cannot use a live-pid comparison here the way
    # systemd/launchd do.
    baseline_data = statusfile.read_status()
    baseline_pid = baseline_data.get("pid") if baseline_data is not None else None

    _win_stop()
    # MUST wait for the task to actually stop reporting RUNNING before
    # issuing /Run -- see `_win_wait_for_task_stopped()`'s docstring for the
    # real-hardware incident this closes (a bare "/Run right after /End"
    # silently lost the restart to IgnoreNew). Never fails the restart over
    # this alone -- honestly reported and the restart is attempted anyway,
    # exactly as `_launchd_restart()` does when `_wait_for_launchd_unload()`
    # times out.
    if not _win_wait_for_task_stopped():
        items.append(
            report.Check(
                "stop",
                report.ACT,
                f"Task did not report stopped within {_WIN_STOP_POLL_TIMEOUT_SECONDS:.0f}s "
                "-- attempting restart anyway",
            )
        )

    result = subprocess.run(
        ["schtasks", "/Run", "/TN", _WIN_TASK_NAME],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        items.append(_failure_check("schtasks", "/Run", result))
        _render_service_report(items)
        sys.exit(1)

    items.append(
        _restart_result_check(
            "Restarted the task",
            fresh_status_check=lambda: _win_wait_for_fresh_status(baseline_pid),
        )
    )
    _render_service_report(items)


def _win_status() -> None:
    """Render our own report -- no external command's output is passed through.

    WINDOWS_NATIVE_SPEC.md section 1.6: `schtasks /Query /V` is explicitly
    rejected as the status source (localized, verbose console text), so
    unlike `_systemd_status()`/`_launchd_status()` there is no richer
    external command worth shelling out to display. Manually assembles
    STATE lines (mixing a `Check` for the task's state with `Readout`s for
    the XML/log paths) rather than going through `render_items()`, the
    same way `cli.status()` does when its output mixes both kinds of line.
    """
    from . import report

    utf8 = report.utf8_capable()
    info = _win_task_query()

    if not info.exists:
        task_glyph = report.ACT
        task_value = "Not registered -- run: muxplex-deck service install"
    elif info.state == _WIN_TASK_STATE_RUNNING:
        # `info.pid` is Task Scheduler's own EnginePID, NOT the sidecar's
        # own pid (see `service_main_pid()`'s docstring) -- labeled
        # explicitly so this line can never be misread as "the sidecar is
        # pid N". That exact misreading (mixed up with `muxplex-deck
        # status`, which shows the sidecar's own self-reported pid) is
        # what made a perfectly healthy sidecar look broken during this
        # port's hardware bring-up.
        pid_text = f" (scheduler engine pid {info.pid})" if info.pid else ""
        task_glyph = report.FINE
        task_value = f"Registered and running{pid_text}"
    else:
        state_text = str(info.state) if info.state is not None else "unknown"
        task_glyph = report.ACT
        task_value = (
            f"Registered but not running (state {state_text}) -- run: "
            "muxplex-deck service logs"
        )

    state_lines = [
        report.format_check_line(task_glyph, "task", task_value, utf8=utf8),
        report.format_readout_line("xml", str(_win_task_xml_path())),
        report.format_readout_line("log", str(_win_default_log_path())),
    ]

    action_count = report.count_actions([task_glyph])
    verdict = report.verdict_readiness(action_count)
    action_lines: list[str] | None = None
    if task_glyph == report.ACT:
        command = report.extract_run_command(task_value)
        decision = report.Decision(commands=[command] if command else [], prose=None)
        action_lines = report.render_action(report.Action(decision=decision))

    sys.stdout.write(report.render(verdict, state_lines, action_lines))


def _win_logs() -> None:
    log_file = _win_default_log_path()
    command = f"Get-Content -LiteralPath '{log_file}' -Tail 50 -Wait"
    try:
        # stdin closed (defense-in-depth, see `_win_install()`'s docstring)
        # -- stdout/stderr stay inherited/unredirected on purpose, this is
        # the raw passthrough stream (module docstring: "logs is -- and
        # stays -- a raw passthrough stream").
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Public API -- platform-dispatching wrappers
#
# Dispatch order is darwin -> windows -> systemd -> unsupported: platform
# IDENTITY beats tool-presence probing (see WINDOWS_NATIVE_SPEC.md section
# 1.6). Today, on Windows, `_have_systemctl()` is always False anyway, so
# this ordering is not currently load-bearing for correctness -- but it is
# the documented, future-proof shape.
# ---------------------------------------------------------------------------


def _unsupported_platform_error(command: str) -> None:
    """Print a clear error when no supported service manager is available."""
    print(
        f"  ERROR: 'muxplex-deck service {command}' requires systemd (Linux), "
        "launchd (macOS), or Task Scheduler (Windows), none of which was found.",
        file=sys.stderr,
    )
    print(
        "  Run muxplex-deck directly to start the sidecar without a service manager:",
        file=sys.stderr,
    )
    print("    muxplex-deck run", file=sys.stderr)


def service_install() -> None:
    """Install the muxplex-deck service/task for the current user."""
    if _is_darwin():
        _launchd_install()
    elif _is_windows():
        _win_install()
    elif _have_systemctl():
        _systemd_install()
    else:
        _unsupported_platform_error("install")


def service_uninstall() -> None:
    """Remove the muxplex-deck service/task for the current user."""
    if _is_darwin():
        _launchd_uninstall()
    elif _is_windows():
        _win_uninstall()
    elif _have_systemctl():
        _systemd_uninstall()
    else:
        _unsupported_platform_error("uninstall")


def service_start() -> None:
    """Start the muxplex-deck service/task."""
    if _is_darwin():
        _launchd_start()
    elif _is_windows():
        _win_start()
    elif _have_systemctl():
        _systemd_start()
    else:
        _unsupported_platform_error("start")


def service_stop() -> None:
    """Stop the muxplex-deck service/task."""
    if _is_darwin():
        _launchd_stop()
    elif _is_windows():
        _win_stop()
    elif _have_systemctl():
        _systemd_stop()
    else:
        _unsupported_platform_error("stop")


def service_restart() -> None:
    """Restart the muxplex-deck service/task."""
    if _is_darwin():
        _launchd_restart()
    elif _is_windows():
        _win_restart()
    elif _have_systemctl():
        _systemd_restart()
    else:
        _unsupported_platform_error("restart")


def service_status() -> None:
    """Print the current status of the muxplex-deck service/task."""
    if _is_darwin():
        _launchd_status()
    elif _is_windows():
        _win_status()
    elif _have_systemctl():
        _systemd_status()
    else:
        _unsupported_platform_error("status")


def service_logs() -> None:
    """Stream or print logs for the muxplex-deck service/task."""
    if _is_darwin():
        _launchd_logs()
    elif _is_windows():
        _win_logs()
    elif _have_systemctl():
        _systemd_logs()
    else:
        _unsupported_platform_error("logs")


# ---------------------------------------------------------------------------
# Cross-platform predicates -- installed / active / main pid
# ---------------------------------------------------------------------------


def service_is_active() -> bool:
    """Best-effort: is the muxplex-deck service/task currently active/running?

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

    if _is_windows():
        info = _win_task_query()
        return info.exists and info.state == _WIN_TASK_STATE_RUNNING

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


def service_main_pid() -> int | None:
    """Best-effort PID of the currently-running muxplex-deck service process.

    Never raises -- matches `service_is_active()`'s contract: any failure
    (service manager missing, service not running, unexpected/unparseable
    output) reads as "cannot determine", which callers must treat as "don't
    know", never as a false positive or negative on its own.

    This is what lets `status()` (and `_wait_for_fresh_status()` below) tell
    whether a published `status.json` (`statusfile.build_status()`'s own
    `pid` field) was written by the process running RIGHT NOW under the
    service, or by a PREVIOUS incarnation whose last write can look
    deceptively fresh by age alone -- see the restart-race incident in
    AGENTS.md: the old process's last write happened moments before it
    exited, so an age-only staleness check saw it as "recent" and reported
    it as current truth. Comparing pids is the only way to tell those apart.

    On Windows this ALWAYS returns None -- VERIFIED FALSE on real hardware
    (2026-07): WINDOWS_NATIVE_SPEC.md section 1.4's item 2 assumed
    `IRunningTask.EnginePID` would equal the sidecar's own pid because the
    task action is a direct `pythonw.exe -m muxplex_deck run` with no
    `cmd.exe` wrapper. A real machine running exactly one healthy sidecar
    (confirmed via its own log: started, connected the deck, polled the
    server, handled key presses) showed `EnginePID` reporting a DIFFERENT
    pid than the one the sidecar itself wrote to `status.json`. This
    matches Microsoft's own documentation, read only after the hardware
    disproved the assumption: `EnginePID` is "the process ID for the
    engine (process) which is running the task"
    (learn.microsoft.com/windows/win32/taskschd/runningtask-enginepid) --
    the Task Scheduler engine HOST process (a shared `svchost.exe -k
    netsvcs -p -s Schedule` on modern Windows, `taskeng.exe` on older
    versions), not the task's own launched process. Independent reports
    going back to 2011 confirm the same thing for direct, unwrapped
    actions, not just batch/script ones. There is no COM property that
    names the sidecar's own live pid, and fabricating one from `EnginePID`
    would be actively wrong, not just imprecise -- so this returns `None`
    (the existing "cannot determine" contract) rather than a value that
    LOOKS authoritative but isn't. Callers must not compare it against
    anything; see `_win_wait_for_fresh_status()` for how the Windows
    restart contract is upheld with a different, genuinely reliable signal.
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
            return None
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("pid = "):
                try:
                    return int(stripped[len("pid = ") :].strip())
                except ValueError:
                    return None
        return None

    if _is_windows():
        # See the docstring above: EnginePID is the Task Scheduler engine
        # HOST process, never the sidecar's own pid. Returning it would be
        # a fabricated, actively-wrong signal -- "cannot determine" (None)
        # is the honest answer, and callers already handle that (`status()`
        # falls back to age-based staleness; `_win_restart()` uses
        # `_win_wait_for_fresh_status()` instead of this function).
        return None

    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                "muxplex-deck",
                "--property=MainPID",
                "--value",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


def service_is_installed() -> bool:
    """Is the muxplex-deck unit/plist/task present?

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
    if _is_windows():
        return _win_task_query().exists
    return _SYSTEMD_UNIT_PATH.exists()
