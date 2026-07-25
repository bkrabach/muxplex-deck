"""muxplex-deck CLI -- sidecar for driving an Elgato Stream Deck against muxplex.

Mirrors muxplex's own CLI shape (`muxplex/cli.py`) so the two tools feel
identical: a default-action root parser (`muxplex-deck` == `muxplex-deck
run`), 3-tier config resolution (CLI flag > config.json > hardcoded
default), a `config` group, a `service` group (systemd/launchd), `doctor`,
and `update`/`upgrade`. Two things are sidecar-specific and have no muxplex
analog: the HID-permission/udev caveat (a service-managed sidecar runs as
a non-root user, which by default cannot open the Stream Deck) and a
standalone `version` command.
"""

from __future__ import annotations

import argparse
import contextlib
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config as config_mod
from .config import DEFAULT_CONFIG, ConfigError

# ---------------------------------------------------------------------------
# Default action: `muxplex-deck` == `muxplex-deck run`
# ---------------------------------------------------------------------------


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    """Add --config, --emulator, --emulator-port to a parser.

    Applied to BOTH the root parser and the `run` subcommand so bare
    `muxplex-deck` and `muxplex-deck run` behave identically. `--config`
    defaults to None so `run()` can distinguish "not passed" from "passed
    the default path" (3-tier resolution -- CLI flag > config.json >
    hardcoded default -- lives inside `config.load_config`, keyed off this
    same None-vs-explicit distinction).
    """
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config JSON file (overrides MUXPLEX_DECK_CONFIG and the "
        "default ~/.config/muxplex-deck/config.json)",
    )
    parser.add_argument(
        "--emulator",
        action="store_true",
        help="Run against the in-process Stream Deck+ emulator (localhost web UI) "
        "instead of real hardware -- no device, no hidapi required.",
    )
    parser.add_argument(
        "--emulator-port",
        type=int,
        default=8484,
        help="Port for the emulator's web UI (default: 8484). Ignored without --emulator.",
    )


def run(
    config_path: str | None = None,
    *,
    emulator: bool = False,
    emulator_port: int = 8484,
) -> int:
    """Load config, build the device backend, and run the sidecar's main loop.

    This is the CLI-level entry ("the default action"); the actual hotplug
    state machine lives in `muxplex_deck.main.run(config, manager)`.
    """
    from . import main as main_mod  # noqa: PLC0415
    from .device import DeviceProbeError  # noqa: PLC0415

    try:
        cfg = config_mod.load_config(config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        manager = main_mod._build_manager(
            emulator=emulator, emulator_port=emulator_port
        )
    except DeviceProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    return main_mod.run(cfg, manager)


# ---------------------------------------------------------------------------
# version
# ---------------------------------------------------------------------------


def get_version() -> str:
    """Return the installed muxplex-deck version, or "dev" if not installed."""
    try:
        from importlib.metadata import version as pkg_version  # noqa: PLC0415

        return pkg_version("muxplex-deck")
    except Exception:
        return "dev"


def print_version() -> None:
    print(f"muxplex-deck {get_version()}")


# ---------------------------------------------------------------------------
# config group -- list / get / set / reset
# ---------------------------------------------------------------------------


def config_list(config_path: str | None = None) -> None:
    """Show all config keys with their current values."""
    settings = config_mod.load_raw_config(config_path)
    resolved_path = config_mod._resolve_config_path(config_path)
    print(f"\nmuxplex-deck config ({resolved_path})\n")

    for key in DEFAULT_CONFIG:
        value = settings.get(key)
        default = DEFAULT_CONFIG[key]
        is_default = value == default
        marker = "" if is_default else " (modified)"
        if isinstance(value, str):
            display = f'"{value}"' if value else '""'
        elif value is None:
            display = "null"
        elif isinstance(value, bool):
            display = "true" if value else "false"
        else:
            display = str(value)
        print(f"  {key}: {display}{marker}")
    print()


def config_get(key: str, config_path: str | None = None) -> None:
    """Show one config value."""
    if key not in DEFAULT_CONFIG:
        print(f"Unknown setting: {key}", file=sys.stderr)
        print(
            f"Valid keys: {', '.join(sorted(DEFAULT_CONFIG.keys()))}", file=sys.stderr
        )
        sys.exit(1)

    settings = config_mod.load_raw_config(config_path)
    value = settings.get(key)
    if isinstance(value, bool):
        print("true" if value else "false")
    elif value is None:
        print("null")
    else:
        print(value)


def config_set(key: str, raw_value: str, config_path: str | None = None) -> None:
    """Set a config value. Type is auto-detected from the default's type."""
    if key not in DEFAULT_CONFIG:
        print(f"Unknown setting: {key}", file=sys.stderr)
        print(
            f"Valid keys: {', '.join(sorted(DEFAULT_CONFIG.keys()))}", file=sys.stderr
        )
        sys.exit(1)

    default = DEFAULT_CONFIG[key]
    value: Any
    try:
        if isinstance(default, bool):
            value = raw_value.lower() in ("true", "1", "yes", "on")
        elif isinstance(default, int) and not isinstance(default, bool):
            value = int(raw_value)
        elif isinstance(default, float):
            value = float(raw_value)
        else:
            value = raw_value
    except ValueError as exc:
        print(f"Invalid value for {key}: {exc}", file=sys.stderr)
        sys.exit(1)

    config_mod.patch_raw_config({key: value}, config_path)
    print(f"  {key}: {value}")


def config_reset(key: str | None = None, config_path: str | None = None) -> None:
    """Reset one or all config keys to defaults."""
    import copy

    if key is not None:
        if key not in DEFAULT_CONFIG:
            print(f"Unknown setting: {key}", file=sys.stderr)
            print(
                f"Valid keys: {', '.join(sorted(DEFAULT_CONFIG.keys()))}",
                file=sys.stderr,
            )
            sys.exit(1)
        config_mod.patch_raw_config({key: DEFAULT_CONFIG[key]}, config_path)
        print(f"  {key} reset to: {DEFAULT_CONFIG[key]}")
    else:
        config_mod.save_raw_config(copy.deepcopy(DEFAULT_CONFIG), config_path)
        resolved_path = config_mod._resolve_config_path(config_path)
        print(f"  All settings reset to defaults ({resolved_path})")


# ---------------------------------------------------------------------------
# doctor -- pure check helpers (each returns (status, message); status is
# "ok" | "warn" | "fail") plus the orchestrating doctor() that prints them.
# Kept pure/injectable so tests can exercise each check without hardware,
# a server, or a service manager.
# ---------------------------------------------------------------------------


def check_python_version() -> tuple[str, str]:
    py_version = platform.python_version()
    ok = tuple(int(x) for x in py_version.split(".")[:2]) >= (3, 11)
    if ok:
        return "ok", f"Python {py_version}"
    return "fail", f"Python {py_version} (3.11+ required)"


def _get_install_info() -> dict:
    """Detect how muxplex-deck was installed using PEP 610 direct_url.json.

    Same shape/logic as muxplex's own `_get_install_info()`, targeting the
    `muxplex-deck` distribution instead.
    """
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    info: dict = {"source": "unknown", "version": "0.0.0", "commit": None, "url": None}
    try:
        dist = distribution("muxplex-deck")
        info["version"] = dist.metadata["Version"]
        du_text = dist.read_text("direct_url.json")
        if du_text:
            du = json.loads(du_text)
            if "vcs_info" in du:
                info["source"] = "git"
                info["commit"] = du["vcs_info"].get("commit_id", "")
                info["url"] = du.get("url", "")
            elif "dir_info" in du and du["dir_info"].get("editable"):
                info["source"] = "editable"
            else:
                info["source"] = "unknown"
        else:
            info["source"] = "pypi"
    except PackageNotFoundError:
        pass
    return info


def _check_for_update(info: dict) -> tuple[bool, str]:
    """Check if an update is available.

    muxplex-deck is git-only (no PyPI release) -- only the git comparison
    path applies. Editable installs are never flagged.
    """
    if info["source"] == "editable":
        return False, "editable install -- manage updates manually"

    if info["source"] == "git":
        try:
            result = subprocess.run(
                ["git", "ls-remote", info["url"], "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return True, "could not check remote -- upgrading to be safe"
            remote_sha = (
                result.stdout.strip().split()[0] if result.stdout.strip() else ""
            )
            local_sha = info["commit"] or ""
            if not remote_sha:
                return True, "could not read remote sha -- upgrading to be safe"
            if local_sha == remote_sha:
                return False, f"up to date (commit {local_sha[:8]})"
            return True, f"update available ({local_sha[:8]} -> {remote_sha[:8]})"
        except Exception:
            return True, "check failed -- upgrading to be safe"

    return True, "unknown install source -- could not check"


def check_install_and_update() -> tuple[str, str]:
    """Combined install-source + update-available check, doctor-line ready."""
    info = _get_install_info()
    commit_suffix = f" @ {info['commit'][:8]}" if info["commit"] else ""
    version_str = get_version()
    update_available, message = _check_for_update(info)
    base = f"muxplex-deck {version_str} (installed via {info['source']}{commit_suffix})"
    if update_available:
        return "warn", f"{base} -- {message} (run: muxplex-deck update)"
    return "ok", f"{base} -- {message}"


def check_config_file(config_path: str | None = None) -> tuple[str, str]:
    resolved = config_mod._resolve_config_path(config_path)
    if resolved.exists():
        return "ok", f"Config: {resolved}"
    return "warn", f"Config: {resolved} (not yet created -- see README.md)"


def check_federation_key(key_file: Path) -> tuple[str, str]:
    """Federation key presence + permission check. Never prints the key itself."""
    if not key_file.exists():
        return "warn", f"Federation key file not found: {key_file}"
    try:
        mode = key_file.stat().st_mode & 0o777
    except OSError as exc:
        return "warn", f"Could not stat federation key file {key_file}: {exc}"
    if mode & 0o077:
        return "warn", (
            f"Federation key file {key_file} is readable by others "
            f"(mode {oct(mode)}) -- run: chmod 600 {key_file}"
        )
    return "ok", f"Federation key: {key_file} (mode {oct(mode)})"


def check_ca_file(ca_file: Path | None) -> tuple[str, str]:
    """ca_file configured, readable, and actually a CA (not a server leaf cert).

    This exact mistake -- pointing ca_file at the server's LEAF certificate
    instead of its CA -- is a known real-world gotcha (see AGENTS.md) that
    manifests as "unable to get local issuer certificate".
    """
    if ca_file is None:
        return "ok", "ca_file: not configured (fine for a publicly-trusted cert)"
    if not ca_file.exists():
        return "warn", f"ca_file configured but not found: {ca_file}"
    try:
        result = subprocess.run(
            [
                "openssl",
                "x509",
                "-in",
                str(ca_file),
                "-noout",
                "-ext",
                "basicConstraints",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return "warn", "openssl not found -- cannot verify ca_file is a CA"
    except Exception as exc:
        return "warn", f"Could not inspect ca_file {ca_file}: {exc}"

    if result.returncode != 0:
        return "warn", f"ca_file {ca_file} does not look like a valid certificate"

    output = result.stdout
    if "CA:TRUE" in output:
        return "ok", f"ca_file is a valid CA: {ca_file}"
    if "CA:FALSE" in output:
        return "warn", (
            f"ca_file {ca_file} has CA:FALSE -- this looks like the server's "
            "LEAF certificate (muxplex.crt), not its CA. Point ca_file at "
            "~/.config/muxplex/ca/muxplex-ca.crt on the server instead, or TLS "
            "verification will fail with 'unable to get local issuer certificate'."
        )
    return (
        "warn",
        f"ca_file {ca_file}: could not determine CA status from basicConstraints",
    )


def probe_deck_status(manager: Any) -> dict:
    """Pure: given a `DeviceManager`-shaped object, find + describe the deck.

    Returns {"found": bool, "openable": bool, "caps": dict | None, "error": str | None}.
    Callers pass a fake manager in tests -- no real hardware required.
    """
    try:
        deck = manager.find_device()
    except Exception as exc:
        return {"found": False, "openable": False, "caps": None, "error": str(exc)}
    if deck is None:
        return {"found": False, "openable": False, "caps": None, "error": None}
    try:
        deck.open()
    except Exception as exc:
        return {"found": True, "openable": False, "caps": None, "error": str(exc)}
    try:
        from deck_probe.capabilities import describe_capabilities  # noqa: PLC0415

        caps = describe_capabilities(deck)
        return {"found": True, "openable": True, "caps": caps, "error": None}
    finally:
        with contextlib.suppress(Exception):
            deck.close()


_NO_DEVICE_GUIDANCE = (
    "No Stream Deck found. Things to check:\n"
    "    - All platforms: close the official Elgato Stream Deck app -- it holds\n"
    "      exclusive HID access, so muxplex-deck cannot open the device while it runs.\n"
    "    - Linux (incl. WSL): a udev rule must grant access to Elgato devices\n"
    "      (vendor id 0x0fd9). Run 'muxplex-deck service install' for the exact\n"
    "      remediation block, or see AGENTS.md.\n"
    "    - WSL specifically: USB devices are not visible until attached from\n"
    "      Windows via usbipd -- in an admin PowerShell:\n"
    "        usbipd list                        (find the Stream Deck's BUSID)\n"
    "        usbipd bind --busid <BUSID>        (first time only)\n"
    "        usbipd attach --wsl --busid <BUSID>\n"
)


def check_deck_detected(config_path: str | None = None) -> tuple[str, str]:
    """Enumerate + describe the connected Stream Deck (real hardware probe)."""
    try:
        from .device import DeviceProbeError  # noqa: PLC0415
        from .device_real import RealDeviceManager  # noqa: PLC0415
    except ImportError as exc:
        return "warn", f"Could not import Stream Deck backend: {exc}"

    try:
        manager = RealDeviceManager()
    except DeviceProbeError as exc:
        return "warn", str(exc)

    status = probe_deck_status(manager)
    if not status["found"]:
        if status["error"]:
            return "warn", f"Stream Deck enumeration failed: {status['error']}"
        return "warn", _NO_DEVICE_GUIDANCE
    caps = status["caps"]
    if caps is None:
        # Found but couldn't open -- reported separately by check_hid_openable.
        return "ok", "Stream Deck detected (not yet opened -- see HID check below)"
    return "ok", (
        f"{caps['model']}: {caps['key_count']} keys "
        f"({caps['key_rows']}x{caps['key_cols']}), {caps['dial_count']} dials, "
        f"touchscreen={'yes' if caps['has_touchscreen'] else 'no'}"
    )


def check_hid_openable() -> tuple[str, str]:
    """Whether the detected Stream Deck can actually be opened (HID permission)."""
    try:
        from .device import DeviceProbeError  # noqa: PLC0415
        from .device_real import RealDeviceManager  # noqa: PLC0415
        from .service import udev_rule_exists  # noqa: PLC0415
    except ImportError as exc:
        return "warn", f"Could not import Stream Deck backend: {exc}"

    try:
        manager = RealDeviceManager()
    except DeviceProbeError as exc:
        return "warn", str(exc)

    status = probe_deck_status(manager)
    if not status["found"]:
        return "warn", "n/a -- no Stream Deck detected"
    if status["openable"]:
        return "ok", "HID: device opened successfully"

    hint = ""
    if sys.platform not in ("darwin", "win32") and not udev_rule_exists():
        hint = " Run: muxplex-deck service install (prints the udev remediation)."
    elif sys.platform not in ("darwin", "win32"):
        hint = " A udev rule exists but the device still could not be opened."
    return "warn", f"HID: could not open device ({status['error']}).{hint}"


def _is_tls_error(exc: Exception) -> bool:
    """Best-effort detection of a TLS/certificate-verification failure.

    httpx surfaces cert failures as a plain `httpx.ConnectError` with the
    underlying SSL error text in the message -- there's no dedicated
    exception type to catch, so string-sniffing is the only option. Shared
    by `check_server_reachable` and the `init` wizard so both agree on what
    counts as "this needs a CA file" versus "server is just unreachable".
    """
    msg = str(exc).lower()
    return "certificate" in msg or "ssl" in msg


def fetch_instance_info(server_url: str, *, verify: bool | str = True) -> dict:
    """GET /api/instance-info and return the parsed JSON body.

    Raises the underlying httpx exception on any failure (timeout, connect
    error, non-2xx status) -- unlike `check_server_reachable`, this is a raw
    data-fetch primitive for callers (like the `init` wizard) that need the
    actual response fields (name, version, federation_enabled), not just a
    doctor-style (status, message) verdict.
    """
    import httpx

    url = f"{server_url.rstrip('/')}/api/instance-info"
    with httpx.Client(verify=verify, timeout=5.0) as client:
        resp = client.get(url)
    resp.raise_for_status()
    return resp.json()


def fetch_ca_cert(server_url: str) -> bytes | None:
    """GET /api/ca (TLS verification disabled for this one bootstrap fetch).

    A CA public certificate is a trust anchor, not a secret -- disabling
    verification only for this single unauthenticated request is safe and
    is what makes self-serving the CA possible at all (the chicken-and-egg
    problem: you can't verify the server until you have its CA). Returns
    the raw cert bytes on 200, or None on 404 (the server has no local CA
    to expose -- e.g. it uses Tailscale/mkcert/a publicly trusted cert,
    which is fine). Any other HTTP or network error is raised.
    """
    import httpx

    url = f"{server_url.rstrip('/')}/api/ca"
    with httpx.Client(verify=False, timeout=5.0) as client:
        resp = client.get(url)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.content


def check_server_reachable(server_url: str, ca_file: Path | None) -> tuple[str, str]:
    """GET /api/instance-info -- the one public, unauthenticated endpoint."""
    if not server_url:
        return "warn", "server_url not configured"

    import httpx

    verify: bool | str = str(ca_file) if ca_file else True
    try:
        data = fetch_instance_info(server_url, verify=verify)
        return (
            "ok",
            f"Server reachable: {data.get('name', '?')} (muxplex {data.get('version', '?')})",
        )
    except httpx.TimeoutException as exc:
        return "warn", f"Server check timed out: {exc}"
    except httpx.ConnectError as exc:
        if _is_tls_error(exc):
            return "warn", (
                f"TLS verification failed: {exc} -- check ca_file points at "
                "the server's CA, not its leaf certificate"
            )
        return "warn", f"Server unreachable: {exc}"
    except Exception as exc:
        return "warn", f"Server check failed: {exc}"


def check_service_status() -> tuple[str, str]:
    """Best-effort service-installed/running check (systemd or launchd)."""
    if sys.platform == "darwin":
        import os as _os

        uid = _os.getuid()
        try:
            result = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/com.muxplex-deck"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            return "warn", "Service: launchctl not found"
        if result.returncode == 0:
            return "ok", "Service: installed (launchd)"
        return "warn", "Service: not installed -- run: muxplex-deck service install"

    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", "muxplex-deck"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return "warn", "Service: systemctl not found -- run muxplex-deck directly"
    if result.returncode == 0:
        return "ok", f"Service: {result.stdout.strip()} (systemd)"
    return "warn", "Service: not installed -- run: muxplex-deck service install"


_CHECK_MARKS: dict[str, str] = {
    "ok": "\033[32m\u2713\033[0m",
    "fail": "\033[31m\u2717\033[0m",
    "warn": "\033[33m!\033[0m",
}


def print_check(status: str, message: str) -> None:
    """Print one doctor-style check line: colored mark, 2-space indent.

    Continuation lines (multi-line messages) are indented 4 spaces with no
    mark. Shared by `doctor()` and the `init` wizard so both surfaces present
    checks identically.
    """
    mark = _CHECK_MARKS.get(status, _CHECK_MARKS["warn"])
    for i, line in enumerate(message.splitlines()):
        prefix = f"  {mark} " if i == 0 else "    "
        print(f"{prefix}{line}")


def doctor(config_path: str | None = None) -> int:
    """Run diagnostic checks and report system status. Always returns 0 (informational)."""
    print("\nmuxplex-deck doctor\n")

    checks: list[tuple[str, str]] = []
    checks.append(check_python_version())
    checks.append(check_install_and_update())
    checks.append(check_config_file(config_path))

    try:
        cfg = config_mod.load_config(config_path)
    except ConfigError:
        cfg = None

    raw = config_mod.load_raw_config(config_path)
    key_file = config_mod._expand(raw.get("key_file", config_mod.DEFAULT_KEY_FILE))
    checks.append(check_federation_key(key_file))

    ca_file = cfg.ca_file if cfg is not None else None
    if cfg is None and raw.get("ca_file"):
        ca_file = config_mod._expand(raw["ca_file"])
    checks.append(check_ca_file(ca_file))

    checks.append(check_deck_detected(config_path))
    checks.append(check_hid_openable())

    server_url = cfg.server_url if cfg is not None else raw.get("server_url", "")
    checks.append(check_server_reachable(server_url, ca_file))

    checks.append(check_service_status())

    for status, message in checks:
        print_check(status, message)

    print()
    return 0


# ---------------------------------------------------------------------------
# update / upgrade
# ---------------------------------------------------------------------------

_REPO_URL = "https://github.com/bkrabach/muxplex-deck"


def _find_uv() -> str | None:
    """Locate `uv`, checking PATH first then well-known install locations."""
    import shutil

    found = shutil.which("uv")
    if found:
        return found
    candidates = [
        str(Path.home() / ".local" / "bin" / "uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
        "/snap/bin/uv",
        "/root/.local/bin/uv",
    ]
    for path in candidates:
        if Path(path).exists() and Path(path).is_file() and _is_executable(path):
            return path
    return None


def _is_executable(path: str) -> bool:
    import os

    return os.access(path, os.X_OK)


def _find_pip() -> str | None:
    import shutil

    for name in ("pip", "pip3"):
        found = shutil.which(name)
        if found:
            return found
    candidates = [
        str(Path.home() / ".local" / "bin" / "pip"),
        str(Path.home() / ".local" / "bin" / "pip3"),
        "/opt/homebrew/bin/pip3",
        "/usr/local/bin/pip3",
    ]
    for path in candidates:
        if Path(path).exists() and _is_executable(path):
            return path
    return None


def _service_is_active() -> bool:
    """Best-effort: is the muxplex-deck service currently running?"""
    if sys.platform == "darwin":
        import os

        uid = os.getuid()
        result = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/com.muxplex-deck"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "muxplex-deck"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def update() -> None:
    """Update muxplex-deck to the latest version and restart the service (if installed).

    Simplified relative to muxplex's own `upgrade()`: muxplex-deck has no
    PyPI release, so there is no "already up to date, use --force" version
    gate -- it always reinstalls from git HEAD (matching `uv tool install
    --force`'s own semantics). Reporting style (plain `  ERROR: ...` on
    stderr equivalent) matches muxplex's.
    """
    from .service import service_install  # noqa: PLC0415

    print("\nmuxplex-deck update\n")

    was_active = _service_is_active()
    if was_active:
        print("  Stopping service...")
        if sys.platform == "darwin":
            import os

            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/com.muxplex-deck"],
                capture_output=True,
            )
        else:
            subprocess.run(
                ["systemctl", "--user", "stop", "muxplex-deck"], capture_output=True
            )
    else:
        print("  No active service found (skipping stop)")

    install_failed = False
    restart_failed = False
    print("  Installing latest version...")
    try:
        uv_path = _find_uv()
        if uv_path:
            result = subprocess.run(
                [uv_path, "tool", "install", "--force", f"git+{_REPO_URL}"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                print(f"  ERROR: uv tool install failed:\n{result.stderr}")
                install_failed = True
            else:
                print("  Installed successfully")
        else:
            pip_path = _find_pip()
            if pip_path:
                result = subprocess.run(
                    [pip_path, "install", "--upgrade", f"git+{_REPO_URL}"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    print(f"  ERROR: pip install failed:\n{result.stderr}")
                    install_failed = True
                else:
                    print("  Installed successfully")
            else:
                print("  ERROR: neither uv nor pip found -- cannot update")
                install_failed = True

        if not install_failed and was_active:
            print("  Regenerating service file...")
            try:
                service_install()
            except Exception as exc:
                print(f"  ERROR: service file regeneration failed: {exc}")
    finally:
        if was_active:
            print("  Restarting service...")
            try:
                from .service import service_restart  # noqa: PLC0415

                service_restart()
            except Exception as exc:
                print(f"  ERROR: service restart failed: {exc}")
                restart_failed = True
            else:
                if not _service_is_active():
                    restart_failed = True

    if install_failed:
        print("\n  ERROR: update failed -- service has been restarted (best-effort).\n")
        sys.exit(1)

    if restart_failed:
        print(
            "\n  ERROR: update installed successfully but the service failed to "
            "restart.\n  The new version is installed but the service is NOT "
            "running.\n  Run: muxplex-deck service start\n"
        )
        sys.exit(1)

    print("\n  Verifying...")
    doctor()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="muxplex-deck",
        description="Drive an Elgato Stream Deck against a muxplex server.",
    )
    _add_run_flags(parser)

    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Run the sidecar (default)")
    _add_run_flags(run_parser)

    sub.add_parser("version", help="Show the muxplex-deck version")

    sub.add_parser("doctor", help="Check dependencies and system status")

    sub.add_parser(
        "update",
        aliases=["upgrade"],
        help="Update muxplex-deck to the latest version and restart the service",
    )

    init_parser = sub.add_parser(
        "init",
        help="Interactive setup wizard: server URL, CA certificate, federation key",
    )
    init_parser.add_argument(
        "server_url",
        nargs="?",
        default=None,
        help="muxplex server URL, e.g. https://your-server:8088 (prompted if omitted)",
    )
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Fail with a clear message instead of prompting when input is needed (for scripting)",
    )

    config_parser = sub.add_parser("config", help="View and manage config")
    config_sub = config_parser.add_subparsers(dest="config_command")
    config_sub.add_parser("list", help="Show all config keys (default)")
    config_get_parser = config_sub.add_parser("get", help="Show one config value")
    config_get_parser.add_argument("key", help="Config key")
    config_set_parser = config_sub.add_parser("set", help="Set a config value")
    config_set_parser.add_argument("key", help="Config key")
    config_set_parser.add_argument("value", help="New value")
    config_reset_parser = config_sub.add_parser("reset", help="Reset to defaults")
    config_reset_parser.add_argument(
        "key", nargs="?", help="Config key (omit to reset all)"
    )

    service_parser = sub.add_parser(
        "service", help="Manage the muxplex-deck background service"
    )
    service_sub = service_parser.add_subparsers(dest="service_command")
    service_sub.add_parser("install", help="Install + enable + start the service")
    service_sub.add_parser("uninstall", help="Stop + disable + remove the service")
    service_sub.add_parser("start", help="Start the service")
    service_sub.add_parser("stop", help="Stop the service")
    service_sub.add_parser("restart", help="Stop + start the service")
    service_sub.add_parser("status", help="Show service status")
    service_sub.add_parser("logs", help="Tail service logs")

    parser.add_argument(
        "--version",
        action="store_true",
        help="Show the muxplex-deck version and exit",
    )

    args = parser.parse_args()

    if getattr(args, "version", False) and args.command is None:
        print_version()
        return

    if args.command == "version":
        print_version()
    elif args.command == "doctor":
        doctor(getattr(args, "config", None))
    elif args.command in ("update", "upgrade"):
        update()
    elif args.command == "init":
        from .init_wizard import run_init  # noqa: PLC0415

        sys.exit(
            run_init(
                getattr(args, "config", None),
                getattr(args, "server_url", None),
                non_interactive=getattr(args, "non_interactive", False),
            )
        )
    elif args.command == "config":
        config_path = getattr(args, "config", None)
        cmd = getattr(args, "config_command", None)
        if cmd == "get":
            config_get(args.key, config_path)
        elif cmd == "set":
            config_set(args.key, args.value, config_path)
        elif cmd == "reset":
            config_reset(getattr(args, "key", None), config_path)
        else:
            config_list(config_path)
    elif args.command == "service":
        from .service import (  # noqa: PLC0415
            service_install,
            service_logs,
            service_restart,
            service_start,
            service_status,
            service_stop,
            service_uninstall,
        )

        cmd = getattr(args, "service_command", None)
        if cmd == "install":
            service_install()
        elif cmd == "uninstall":
            service_uninstall()
        elif cmd == "start":
            service_start()
        elif cmd == "stop":
            service_stop()
        elif cmd == "restart":
            service_restart()
        elif cmd == "status":
            service_status()
        elif cmd == "logs":
            service_logs()
        else:
            service_parser.print_help()
    else:
        # No subcommand (or "run"): the default action.
        sys.exit(
            run(
                args.config,
                emulator=args.emulator,
                emulator_port=args.emulator_port,
            )
        )


if __name__ == "__main__":
    main()
