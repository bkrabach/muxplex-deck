"""The single home for every WSL / udev / HID-permission guidance string.

Imported by `cli`, `service`, `main`, and `init_wizard` -- no surface
composes its own copy of this text (see WSL_COLD_START_SPEC.md P6). This
module classifies the environment (`explain_environment`), explains a
specific open failure (`explain_open_failure`), and replaces
`service`'s old `_UDEV_REMEDIATION` constant (`udev_guidance`).

Design principles this module encodes (see the spec for the full
rationale):

- P1: the permission remediation branches on capability
  (`usbnode.udev_is_live()`), never on platform name -- this is why it
  also repairs plain-Linux containers, not just WSL.
- P3: nothing here ever invokes `sudo` (or anything else). Every
  privileged command is a string to copy-paste, never executed.
- P4: never print a command known to fail on this machine. When udev
  isn't live, `udev_guidance()` returns `None` instead of a caveated
  warning.
- P5: every placeholder we can resolve, we resolve. `<BUSID>` and
  `<NODE>` never appear when the real value was available.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from . import usbnode, wsl

__all__ = [
    "HID_HINT_RULE_EXISTS_BUT_STILL_FAILED",
    "HID_HINT_RUN_SERVICE_INSTALL",
    "Guidance",
    "explain_environment",
    "explain_open_failure",
    "udev_guidance",
]

_ELGATO_VENDOR_ID = "0fd9"

# Short hints appended to `cli.check_hid_openable()`'s "could not open
# device" warning. Kept here (P6: one home for the strings) even though
# the decision of *which* one applies stays in `cli.py` -- that decision
# depends only on `service.udev_rule_exists()` (a filesystem check for a
# rule *file*, unrelated to whether udev is *running*), so it doesn't need
# this module's live-environment probing to stay correct.
HID_HINT_RUN_SERVICE_INSTALL = (
    " Run: muxplex-deck service install (prints the udev remediation)."
)
HID_HINT_RULE_EXISTS_BUT_STILL_FAILED = (
    " A udev rule exists but the device still could not be opened."
)

_UDEV_RULE_CONTENT = (
    'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0660", '
    'GROUP="plugdev", TAG+="uaccess"'
)


@dataclass(frozen=True)
class Guidance:
    """One piece of environment guidance, ready for `cli.print_check`-style display."""

    status: str  # "ok" | "warn" | "fail" -- matches cli.print_check
    message: str  # multi-line; caller does the indenting
    state: str  # "W4", "U-DEAD", ... for tests + telemetry


def _resolve_owner_name(uid: int | None) -> str:
    if uid is None:
        return "?"
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError, OSError):
        return str(uid)


def _format_node_permissions(node: usbnode.UsbNode) -> str:
    """Render the node's type+permission bits `ls -l`-style, plus its owner."""
    if node.mode is None:
        return f"{node.path} (permissions unknown)"
    import stat as stat_mod

    filemode = stat_mod.filemode(stat_mod.S_IFCHR | node.mode)
    owner = _resolve_owner_name(node.owner_uid)
    return f"{node.path}   ({filemode} owned by {owner})"


# ---------------------------------------------------------------------------
# Per-state message bodies (WSL_COLD_START_SPEC.md section 7)
# ---------------------------------------------------------------------------

W2_MESSAGE = (
    "WSL detected -- USB devices are invisible to Linux until Windows hands them over,\n"
    "and I can't find usbipd.exe to check or do that for you.\n"
    "On Windows, in any PowerShell, install the bridge once:\n"
    "    winget install --interactive --exact dorssel.usbipd-win\n"
    "Then come back here and run:\n"
    "    muxplex-deck wsl attach\n"
    "If usbipd-win IS already installed, WSL's Windows-interop is probably switched\n"
    'off for this distro -- see docs/WSL.md ("usbipd.exe not found").'
)

W1_MESSAGE = (
    "This is WSL1 -- USB/IP device passthrough (usbipd-win) requires WSL2.\n"
    "Upgrade this distro from an elevated Windows PowerShell:\n"
    "    wsl.exe --set-version <distro-name> 2\n"
    "(Find <distro-name> with: wsl.exe -l -v)"
)

NO_MATCHING_WINDOWS_DEVICE_MESSAGE = (
    "No Stream Deck visible to Windows' USB/IP bridge (usbipd.exe list shows nothing\n"
    "with vendor id 0fd9). Check the USB cable, try a different port, and check it\n"
    "shows up in Windows Device Manager."
)


def w4_message(device: wsl.UsbipdDevice) -> str:
    return (
        f"Stream Deck is plugged into Windows (BUSID {device.busid}, {device.vid_pid}) "
        "but not shared with WSL.\n"
        "Sharing needs administrator rights, which I can't get for you. On Windows, open\n"
        "PowerShell **as Administrator** and run:\n"
        f"    usbipd.exe bind --busid {device.busid}\n"
        "Close the Elgato Stream Deck app first -- it holds the device open.\n"
        "Then come back here (no admin needed) and run:\n"
        "    muxplex-deck wsl attach"
    )


def w5_message(device: wsl.UsbipdDevice) -> str:
    return (
        f"Stream Deck is shared but not attached to WSL (BUSID {device.busid}).\n"
        "Run:\n"
        "    muxplex-deck wsl attach\n"
        "That's it -- no administrator rights needed for this step."
    )


def w6_message(device: wsl.UsbipdDevice) -> str:
    return (
        f"Stream Deck shows Attached in usbipd.exe (BUSID {device.busid}) but Linux does\n"
        "not see it on the USB bus yet. This can happen right after attaching, if it's\n"
        "attached to a different WSL distro, or after a vhci glitch. Try:\n"
        "    muxplex-deck wsl attach\n"
        "If that doesn't help, on Windows: usbipd.exe detach --busid "
        f"{device.busid}, then re-attach."
    )


def w3_unknown_state_message(device: wsl.UsbipdDevice) -> str:
    return (
        f"Stream Deck found (BUSID {device.busid}, {device.vid_pid}) but its usbipd.exe\n"
        "state could not be parsed. Run `usbipd.exe list` yourself and check the STATE\n"
        "column, or just try:\n"
        "    muxplex-deck wsl attach"
    )


def w7_message(node: usbnode.UsbNode) -> str:
    rendered = _format_node_permissions(node)
    owner = _resolve_owner_name(node.owner_uid)
    return (
        "Stream Deck is attached, but this user can't open it.\n"
        f"    Device node: {rendered}\n"
        "Grant yourself access:\n"
        f'    sudo chown "$(id -un)" {node.path}\n'
        "Then:\n"
        "    muxplex-deck service restart\n"
        "Heads up: the device number changes on EVERY attach, so this is a per-attach step.\n"
        "After any unplug, or after a Windows reboot: muxplex-deck wsl attach, then chown again."
        + (f"\n(Current owner: {owner})" if owner not in ("?", "root") else "")
    )


def u_dead_wsl_message(state: str) -> str:
    if state == "enabled":
        return (
            "udev is not running (no /run/udev/control) even though /etc/wsl.conf already has\n"
            "systemd=true. The distro probably hasn't been restarted since that was set.\n"
            "In Windows PowerShell:  wsl.exe --shutdown\n"
            "Then reopen this distro and run:  muxplex-deck wsl attach\n"
            "Until then, the per-attach `sudo chown` above is the way through."
        )

    if state == "boot-section-exists":
        edit_line = (
            "Add `systemd=true` under the existing [boot] section in /etc/wsl.conf:\n"
            "    sudo nano /etc/wsl.conf"
        )
    else:
        edit_line = "    printf '[boot]\\nsystemd=true\\n' | sudo tee -a /etc/wsl.conf >/dev/null"

    return (
        "udev is not running here (no /run/udev/control), so udev rules will never fire --\n"
        "this is normal for a WSL distro without systemd, and for containers.\n"
        'That\'s why "udevadm control --reload-rules" fails with "No such file or directory".\n'
        "Durable fix -- turn on systemd (which starts udev):\n"
        f"{edit_line}\n"
        "Then, in Windows PowerShell:\n"
        "    wsl.exe --shutdown\n"
        "Reopen this distro and run:  muxplex-deck wsl attach\n"
        "You can skip all of that -- the per-attach `sudo chown` above works fine on its own."
    )


def u_dead_container_message() -> str:
    return (
        "udev is not running here (no /run/udev/control), so udev rules will never fire --\n"
        "this is normal for containers and minimal images without systemd/udev.\n"
        "The udev-based remediation would never take effect here, so it's skipped.\n"
        "Grant access to the Stream Deck's device node directly once you know it, e.g.:\n"
        '    sudo chown "$(id -un)" /dev/bus/usb/BBB/DDD\n'
        "or run the service as a user/group that already has access to it."
    )


def impostor_message(paths: wsl.UsbipdPaths) -> str:
    return (
        "Two different programs named `usbipd` are on your PATH:\n"
        f"    {paths.linux_impostor}"
        "                              <- Linux USB/IP daemon (linux-tools-common)\n"
        f"    {paths.windows}   <- the Windows bridge -- THIS is the one\n"
        "Always type `usbipd.exe`. The bare name is the Linux one; it will tell you to install\n"
        "`linux-tools-<kernel>`, a package that does not exist for the WSL kernel.\n"
        "(Or just use `muxplex-deck wsl attach` and never type either.)"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def explain_environment(*, allow_usbipd_query: bool = True) -> list[Guidance]:
    """Classify the current environment and return actionable guidance.

    Returns an empty list when there is nothing to say -- this is the
    common case (macOS, or healthy native Linux with udev running and a
    working device) and is what keeps `doctor()`'s output unchanged for
    those users (no new noise).

    `allow_usbipd_query=False` is the escape hatch for contexts that must
    not shell out (see WSL_COLD_START_SPEC.md section 6.1's per-command
    boundary table).
    """
    guidances: list[Guidance] = []
    info = wsl.detect()

    if not info.is_wsl:
        if sys.platform not in ("darwin", "win32") and not usbnode.udev_is_live():
            guidances.append(
                Guidance(
                    status="warn", message=u_dead_container_message(), state="U-DEAD"
                )
            )
        return guidances

    if info.version == 1:
        guidances.append(Guidance(status="fail", message=W1_MESSAGE, state="W1"))
        return guidances

    paths = wsl.find_usbipd()
    if paths.windows is not None and paths.linux_impostor is not None:
        guidances.append(
            Guidance(status="warn", message=impostor_message(paths), state="IMPOSTOR")
        )

    if paths.windows is None:
        guidances.append(Guidance(status="warn", message=W2_MESSAGE, state="W2"))
        return guidances

    if not allow_usbipd_query:
        return guidances

    devices = wsl.list_devices(paths.windows, vendor_id=_ELGATO_VENDOR_ID)
    if devices is None:
        guidances.append(Guidance(status="warn", message=W2_MESSAGE, state="W2"))
        return guidances

    if not devices:
        guidances.append(
            Guidance(
                status="warn", message=NO_MATCHING_WINDOWS_DEVICE_MESSAGE, state="W3"
            )
        )
        return guidances

    device = devices[0]

    if device.state == "not_shared":
        guidances.append(
            Guidance(status="warn", message=w4_message(device), state="W4")
        )
        return guidances

    if device.state == "shared":
        guidances.append(
            Guidance(status="warn", message=w5_message(device), state="W5")
        )
        return guidances

    if device.state == "unknown":
        guidances.append(
            Guidance(
                status="warn", message=w3_unknown_state_message(device), state="W3"
            )
        )
        return guidances

    # device.state == "attached"
    node = usbnode.find_usb_node(_ELGATO_VENDOR_ID)
    if node is None:
        guidances.append(
            Guidance(status="warn", message=w6_message(device), state="W6")
        )
        return guidances

    if not node.readable_writable:
        guidances.append(Guidance(status="warn", message=w7_message(node), state="W7"))
        if not usbnode.udev_is_live():
            systemd_state = wsl.wsl_conf_systemd_state()
            guidances.append(
                Guidance(
                    status="warn",
                    message=u_dead_wsl_message(systemd_state),
                    state="U-DEAD",
                )
            )
        return guidances

    return guidances


def explain_open_failure(error: str, *, allow_usbipd_query: bool = True) -> Guidance:
    """One combined Guidance explaining why opening the Stream Deck just failed.

    Called once per failure *episode* (never per poll cycle -- see
    `main.py`'s log-once-not-per-cycle discipline). Falls back to a
    minimal, still-honest message when the environment classifier itself
    has nothing more specific to say (e.g. some other process holds the
    device on a healthy platform).
    """
    guidances = explain_environment(allow_usbipd_query=allow_usbipd_query)
    if not guidances:
        return Guidance(
            status="warn",
            message=f"cannot open the Stream Deck: {error}",
            state="W8",
        )
    combined = "\n".join(g.message for g in guidances)
    return Guidance(
        status=guidances[0].status, message=combined, state=guidances[0].state
    )


def is_wsl2() -> bool:
    """True under WSL2 specifically (not WSL1, not non-WSL).

    Used by `init_wizard` to decide whether offering `muxplex-deck wsl
    attach` makes sense at all -- WSL1 can't use USB/IP, and offering it
    on macOS/native Linux would be pure noise.
    """
    return wsl.detect().version == 2


def udev_guidance() -> Guidance | None:
    """Guidance for `service install`'s "no udev rule found" warning.

    Returns `None` when udev is not live (P4: never print a command we
    know will fail here) -- `explain_environment()`'s U-DEAD guidance
    takes its place in that case instead. Also returns `None` on WSL:
    `udevadm control --reload-rules` can report success there while the
    rule still never fires for a usbip-attached device (see AGENTS.md
    "U7" -- a real WSL user followed this exact block and lost ~40
    minutes to a rule that never took effect). On WSL the only proven
    remediation is the per-attach `sudo chown` in `w7_message()`, already
    surfaced by `explain_environment()` -- callers show that instead (see
    `service._warn_if_no_udev_rule`).

    The install command is a single `echo | sudo tee` line rather than a
    `<<'EOF' ... EOF` heredoc: this text gets copy-pasted into a terminal,
    and a heredoc terminator that isn't at column 0 (as it wasn't here,
    once the surrounding block got indented for display) is silently
    swallowed by the shell, leaving the user stuck at a `>` continuation
    prompt. A single line has no terminator to misplace.
    """
    if wsl.detect().is_wsl:
        return None
    if not usbnode.udev_is_live():
        return None
    return Guidance(
        status="warn",
        message=(
            f"No udev rule found for the Stream Deck (vendor id {_ELGATO_VENDOR_ID}).\n"
            "Without it, the service (running as your user, not root) will fail to\n"
            "open the device. Install a rule once:\n\n"
            f"      echo '{_UDEV_RULE_CONTENT}' | sudo tee "
            "/etc/udev/rules.d/70-streamdeck.rules >/dev/null\n"
            "      sudo udevadm control --reload-rules && sudo udevadm trigger\n\n"
            "Then unplug and replug the Stream Deck (or re-run `muxplex-deck wsl attach`\n"
            "under WSL), and make sure you're in the `plugdev` group:\n"
            '      sudo usermod -aG plugdev "$(id -un)"     # then log out and back in'
        ),
        state="U-LIVE",
    )
