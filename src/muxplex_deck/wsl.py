"""WSL detection + usbipd-win facts. No prose here -- see `hidhelp.py`.

Every function in this module is a pure fact-gatherer except `attach()`,
which is the ONE function anywhere in the WSL-guidance surface that
mutates anything (it hands a USB device from Windows to this WSL distro).
Everything else only reads.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "UsbipdDevice",
    "UsbipdPaths",
    "WslInfo",
    "attach",
    "detect",
    "find_usbipd",
    "list_devices",
    "wsl_conf_systemd_state",
]


@dataclass(frozen=True)
class WslInfo:
    is_wsl: bool
    version: int | None  # 1, 2, or None (not WSL)
    kernel: str


@dataclass(frozen=True)
class UsbipdPaths:
    windows: Path | None
    linux_impostor: Path | None


@dataclass(frozen=True)
class UsbipdDevice:
    busid: str
    vid_pid: str
    description: str
    state: str  # "not_shared" | "shared" | "attached" | "unknown"


def detect(*, osrelease_path: Path = Path("/proc/sys/kernel/osrelease")) -> WslInfo:
    """Detect WSL + version from `/proc/sys/kernel/osrelease` ONLY.

    Deliberately does NOT use `WSL_DISTRO_NAME` / `WSL_INTEROP`: those are
    shell-injected environment variables, absent under the systemd user
    unit the sidecar actually runs in -- exactly the context this needs
    to work in.
    """
    try:
        text = osrelease_path.read_text(encoding="utf-8").strip()
    except OSError:
        return WslInfo(is_wsl=False, version=None, kernel="")

    lowered = text.lower()
    if "microsoft" not in lowered:
        return WslInfo(is_wsl=False, version=None, kernel=text)

    version = 2 if "wsl2" in lowered else 1
    return WslInfo(is_wsl=True, version=version, kernel=text)


def find_usbipd() -> UsbipdPaths:
    """Resolve both `usbipd.exe` (the Windows bridge) and a same-named Linux binary.

    If a bare `usbipd` resolves to something other than `usbipd.exe`, it is
    the Linux USB/IP daemon (from `linux-tools-common`) -- an impostor for
    our purposes, since it cannot see Windows-attached devices at all.
    """
    windows = shutil.which("usbipd.exe")
    linux = shutil.which("usbipd")
    impostor = Path(linux) if linux and linux != windows else None
    return UsbipdPaths(
        windows=Path(windows) if windows else None,
        linux_impostor=impostor,
    )


def _parse_list_output(text: str, vendor_id: str) -> list[UsbipdDevice]:
    """Parse the `Connected:` section of `usbipd.exe list` output.

    Only reads BUSID, VID:PID, and the trailing STATE word(s) -- the three
    fields that cannot be truncated (usbipd-win truncates only the DEVICE
    description column, which is ignored here). State is matched
    case-insensitively against {"not shared", "shared", "attached"};
    anything else degrades to "unknown" rather than guessing.
    """
    vendor_lower = vendor_id.lower()
    devices: list[UsbipdDevice] = []
    in_connected = False

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("Connected:"):
            in_connected = True
            continue
        if stripped.startswith("Persisted:"):
            in_connected = False
            continue
        if not in_connected:
            continue
        if stripped.upper().startswith("BUSID"):
            continue  # header row

        parts = stripped.split()
        if len(parts) < 3:
            continue
        busid, vid_pid = parts[0], parts[1]
        if vendor_lower not in vid_pid.lower():
            continue

        rest = parts[2:]
        if (
            len(rest) >= 2
            and rest[-2].lower() == "not"
            and rest[-1].lower() == "shared"
        ):
            state = "not_shared"
            description = " ".join(rest[:-2])
        elif rest and rest[-1].lower() == "shared":
            state = "shared"
            description = " ".join(rest[:-1])
        elif rest and rest[-1].lower() == "attached":
            state = "attached"
            description = " ".join(rest[:-1])
        else:
            state = "unknown"
            description = " ".join(rest)

        devices.append(
            UsbipdDevice(
                busid=busid, vid_pid=vid_pid, description=description, state=state
            )
        )

    return devices


def list_devices(
    usbipd: Path, *, vendor_id: str = "0fd9", timeout: float = 5.0
) -> list[UsbipdDevice] | None:
    """Run `usbipd.exe list` and parse devices matching `vendor_id`.

    Returns `None` (not `[]`) on timeout / `FileNotFoundError` / `OSError`
    (including "Exec format error" when WSL interop is disabled) -- `None`
    means "could not query", `[]` means "queried, nothing connected." The
    caller must treat these differently (W2 vs W3).
    """
    try:
        result = subprocess.run(
            [str(usbipd), "list"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse_list_output(result.stdout, vendor_id)


def attach(usbipd: Path, busid: str, *, timeout: float = 30.0) -> tuple[bool, str]:
    """Attach `busid` to this WSL distro. The ONLY mutating function here.

    Returns `(success, message)`. `message` is usbipd.exe's own stdout on
    success, or its stderr (falling back to stdout, then the exception
    text) on failure -- never fabricated.
    """
    try:
        result = subprocess.run(
            [str(usbipd), "attach", "--wsl", "--busid", busid],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, result.stdout.strip()
    return False, (result.stderr or result.stdout or "").strip()


def wsl_conf_systemd_state(*, wsl_conf_path: Path = Path("/etc/wsl.conf")) -> str:
    """Classify `/etc/wsl.conf`'s systemd setting: "enabled" | "boot-section-exists" | "absent" | "unreadable".

    Lets the caller print *append* vs *edit* correctly instead of a blind
    `tee -a` that could produce a duplicate `[boot]` section.
    """
    try:
        text = wsl_conf_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unreadable"

    in_boot = False
    has_boot_section = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            in_boot = line[1:-1].strip().lower() == "boot"
            has_boot_section = has_boot_section or in_boot
            continue
        if in_boot and "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() == "systemd" and value.strip().lower() == "true":
                return "enabled"

    return "boot-section-exists" if has_boot_section else "absent"
