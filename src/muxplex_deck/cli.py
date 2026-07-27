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
import json
import platform
import subprocess
import sys
import time
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
    from . import main as main_mod
    from .device import DeviceProbeError

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
        from importlib.metadata import version as pkg_version

        return pkg_version("muxplex-deck")
    except Exception:  # noqa: BLE001 -- importlib.metadata can fail in several ways when not installed
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


_PYPI_PROJECT_URL = "https://pypi.org/pypi/muxplex-deck/json"


def _check_for_update(info: dict) -> tuple[bool, str]:
    """Check if an update is available.

    muxplex-deck 0.4.0+ is published to PyPI, so both the git and pypi
    comparison paths apply now. Editable installs are never flagged.
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
                check=False,
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
        except Exception:  # noqa: BLE001 -- any git/network failure means "upgrade to be safe"
            return True, "check failed -- upgrading to be safe"

    if info["source"] == "pypi":
        try:
            import httpx

            response = httpx.get(
                _PYPI_PROJECT_URL,
                headers={"Accept": "application/json"},
                timeout=10,
            )
            response.raise_for_status()
            latest = response.json()["info"]["version"]
            current = info["version"]
            if latest == current:
                return False, f"up to date (v{current})"
            return True, f"update available (v{current} -> v{latest})"
        except Exception:  # noqa: BLE001 -- any network/parse failure means "upgrade to be safe"
            return True, "could not check PyPI -- upgrading to be safe"

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
    return "warn", f"Config: {resolved} (not yet created -- run: muxplex-deck init)"


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
            check=False,
        )
    except FileNotFoundError:
        return "warn", "openssl not found -- cannot verify ca_file is a CA"
    except Exception as exc:  # noqa: BLE001 -- doctor check must degrade to warn, never raise
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
    except Exception as exc:  # noqa: BLE001 -- HID backends raise varied errors; report, don't crash
        return {"found": False, "openable": False, "caps": None, "error": str(exc)}
    if deck is None:
        return {"found": False, "openable": False, "caps": None, "error": None}
    try:
        deck.open()
    except Exception as exc:  # noqa: BLE001 -- HID backends raise varied errors; report, don't crash
        return {"found": True, "openable": False, "caps": None, "error": str(exc)}
    try:
        from deck_probe.capabilities import describe_capabilities

        caps = describe_capabilities(deck)
        return {"found": True, "openable": True, "caps": caps, "error": None}
    finally:
        with contextlib.suppress(Exception):
            deck.close()


_NO_DEVICE_GUIDANCE = (
    "No Stream Deck found. Things to check:\n"
    "    - Close the official Elgato Stream Deck app -- it holds exclusive HID access,\n"
    "      so muxplex-deck cannot open the device while it is running.\n"
    "    - Check the USB cable and try a different port.\n"
)


def check_deck_detected(config_path: str | None = None) -> tuple[str, str]:
    """Enumerate + describe the connected Stream Deck (real hardware probe)."""
    try:
        from .device import DeviceProbeError
        from .device_real import RealDeviceManager
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
    """Whether the detected Stream Deck can actually be opened (HID permission).

    HID access is exclusive: once the muxplex-deck *service* is running, it
    holds the device's handle for the whole time it's active, so ANY second
    process (including this very check) will fail to open it -- that is
    correct, expected behavior, not a bug. The robust way to tell "someone
    else has it because our own service is healthy" apart from a genuine
    permission problem is to check SERVICE STATE, not the open() error text:
    hidapi/libusb error strings vary by platform, library version, and
    locale, so pattern-matching on them is unreliable and unverifiable
    without hardware on every platform. Do NOT "fix" this back into a plain
    warning by reintroducing string-matching -- that regresses the exact
    false-positive this check exists to avoid.
    """
    try:
        from .device import DeviceProbeError
        from .device_real import RealDeviceManager
        from .service import service_is_active, udev_rule_exists
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

    # Open failed. Check service state BEFORE treating this as an error --
    # see the docstring above for why this must key off service state, not
    # the error string.
    if service_is_active():
        return "ok", "HID: device in use by the muxplex-deck service (expected)"

    from . import hidhelp
    from . import wsl as wsl_mod

    hint = ""
    if wsl_mod.detect().is_wsl:
        # The udev-rule hints below don't apply on WSL -- see
        # hidhelp.udev_guidance()'s WSL gate. explain_environment()'s W7
        # guidance (the proven per-attach chown), already surfaced above
        # this line in `doctor()`, covers this instead.
        pass
    elif sys.platform not in ("darwin", "win32") and not udev_rule_exists():
        hint = hidhelp.HID_HINT_RUN_SERVICE_INSTALL
    elif sys.platform not in ("darwin", "win32"):
        hint = hidhelp.HID_HINT_RULE_EXISTS_BUT_STILL_FAILED
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
    except Exception as exc:  # noqa: BLE001 -- catch-all after explicit httpx cases; doctor never raises
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
                check=False,
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
            check=False,
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

    # Environment guidance (WSL/usbipd/udev-liveness) BEFORE the device
    # checks -- it explains why the next line is about to warn. Returns []
    # on a healthy platform (macOS, or native Linux with udev running), so
    # this adds no output there -- see hidhelp.explain_environment().
    from . import hidhelp

    env_guidances = hidhelp.explain_environment()
    for guidance in env_guidances:
        checks.append((guidance.status, guidance.message))
    env_states = {g.state for g in env_guidances}

    if "W7" in env_states:
        # The W7 guidance just appended above already says the device is
        # attached but can't be opened, plus the proven per-attach `chown`
        # fix -- check_deck_detected()/check_hid_openable() would only
        # restate the same fact across two more (partly contradictory)
        # lines. See the WSL cold-start bug report: "3 lines describe one
        # problem". Skip both; nothing more to add.
        pass
    else:
        deck_status, deck_message = check_deck_detected(config_path)
        if deck_message == _NO_DEVICE_GUIDANCE and env_states & {
            "W1",
            "W2",
            "W3",
            "W4",
            "W5",
            "W6",
        }:
            # A WSL-specific state above already explains exactly where
            # the device is (or precisely why it isn't visible to this OS
            # yet) -- the generic "check the cable" guidance would
            # flatly contradict it (see bug report: located @ BUSID 1-4,
            # immediately followed by "check your cable").
            deck_message = (
                "Stream Deck: not detected on this OS yet -- see the WSL "
                "guidance above."
            )
        checks.append((deck_status, deck_message))
        checks.append(check_hid_openable())

    server_url = cfg.server_url if cfg is not None else raw.get("server_url", "")
    checks.append(check_server_reachable(server_url, ca_file))

    checks.append(check_service_status())

    for status, message in checks:
        print_check(status, message)

    print()
    return 0


# ---------------------------------------------------------------------------
# status -- hardware + connection view, read from the running sidecar's
# published status file (see .statusfile). Never contends with the sidecar
# for the exclusive HID handle: `doctor`'s check_hid_openable() probes the
# device directly and can produce an expected false failure once the
# service holds it (see that function's docstring); `status` avoids the
# problem entirely by reading what the running process already knows.
# ---------------------------------------------------------------------------

# A "small multiple" of the sidecar's default 2s poll interval -- generous
# enough to absorb scheduling jitter, but tight enough that a genuinely
# stuck/dead sidecar is flagged well before a human would otherwise notice.
_STATUS_STALE_THRESHOLD_SECONDS = 15.0


def _format_device_line(device: dict[str, Any]) -> tuple[str, str]:
    if not device.get("connected"):
        return "warn", "Device: not connected"
    caps = device.get("capabilities") or {}
    hint = device.get("hint")
    if not caps and hint:
        return "warn", "Device: connected (capabilities unavailable)"
    if not caps:
        return "ok", "Device: connected (capabilities unavailable)"
    touchscreen = "yes" if caps.get("has_touchscreen") else "no"
    return "ok", (
        f"Device: {caps.get('model', '?')} -- {caps.get('key_count', '?')} keys "
        f"({caps.get('key_rows', '?')}x{caps.get('key_cols', '?')}), "
        f"{caps.get('dial_count', '?')} dials, touchscreen={touchscreen}"
    )


def _format_server_line(server: dict[str, Any]) -> tuple[str, str]:
    url = server.get("url") or "(not configured)"
    if server.get("connected"):
        return "ok", f"Server: {url} (reachable)"
    err = server.get("last_error")
    message = f"Server: {url} (unreachable)"
    if err:
        message += f" -- {err}"
    return "warn", message


def _format_state_line(state: dict[str, Any]) -> str:
    session = state.get("active_session") or "none"
    view = state.get("active_view") or "all"
    page = state.get("page")
    page_text = str(page) if page is not None else "-"
    return f"Active session: {session} | view: {view} | page: {page_text}"


def _print_direct_probe(config_path: str | None) -> None:
    """Fallback when the service isn't running -- nothing holds the device."""
    print_check(*check_deck_detected(config_path))


def status(config_path: str | None = None, *, as_json: bool = False) -> int:
    """Print the sidecar's hardware + connection status.

    Reads the status file the running sidecar publishes (see
    `.statusfile`) rather than probing the device directly -- HID access is
    exclusive, so a direct probe would produce a false "could not open"
    failure whenever the service is healthy and already holds the device
    (exactly the false-failure `check_hid_openable` now also avoids). When
    the service is NOT running, nothing holds the device, so it's safe to
    fall back to a direct probe -- this keeps `status` useful even before
    the service has ever been installed.
    """
    from .service import service_is_active
    from .statusfile import read_status

    running = service_is_active()
    data = read_status()

    if as_json:
        payload: dict[str, Any] = {"service_running": running, "status": data}
        print(json.dumps(payload, indent=2))
        return 0 if data is not None else 1

    print("\nmuxplex-deck status\n")
    print_check(
        "ok" if running else "warn",
        f"Service: {'running' if running else 'not running'}",
    )

    if not running:
        if data is None:
            print_check(
                "warn",
                "No status file found -- probing the device directly instead "
                "(safe: nothing holds it while the service is stopped).",
            )
        else:
            print_check(
                "warn",
                "Service not running -- probing the device directly instead "
                "of trusting the (possibly stale) status file.",
            )
        _print_direct_probe(config_path)
        print()
        return 0

    if data is None:
        print_check(
            "warn",
            "No status file found even though the service is running -- it "
            "may have just started. Try: muxplex-deck service logs",
        )
        print()
        return 0

    age = time.time() - data.get("updated_at", 0)
    if age > _STATUS_STALE_THRESHOLD_SECONDS:
        print_check(
            "warn",
            f"Status file is stale (last updated {age:.0f}s ago) -- the "
            "sidecar may be stuck. Try: muxplex-deck service logs",
        )
    else:
        print_check("ok", f"Status updated {age:.0f}s ago (pid {data.get('pid', '?')})")

    device_data = data.get("device", {})
    print_check(*_format_device_line(device_data))
    hint = device_data.get("hint")
    if hint and not device_data.get("connected"):
        # Populated by the sidecar's open-failure branch (see main.py /
        # hidhelp.explain_open_failure) -- this is what turns a stale
        # status file into the primary teaching surface instead of a
        # bare "not connected" with no explanation.
        print_check("warn", hint)
    print_check(*_format_server_line(data.get("server", {})))
    print_check("ok", _format_state_line(data.get("state", {})))

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
    """Best-effort: is the muxplex-deck service currently running?

    Thin wrapper around `service.service_is_active` -- the real
    implementation now lives there (shared with `check_hid_openable`, which
    needs it to tell "our own service holds the device" apart from a
    genuine HID-permission failure). Kept as a module-level name here so
    existing call sites/tests can monkeypatch `cli._service_is_active`
    directly without needing to know it delegates.
    """
    from .service import service_is_active

    return service_is_active()


def update(*, force: bool = False) -> None:
    """Update muxplex-deck to the latest version and restart the service (if installed).

    Respects how the tool was installed (see `_get_install_info`), mirroring
    muxplex's own `upgrade()`: a `pypi` install upgrades from PyPI (package
    name, so `uv tool install --force muxplex-deck` / `pip install --upgrade
    muxplex-deck`); a `git` (or `unknown`) install keeps reinstalling from
    git HEAD via `_REPO_URL`, exactly as before -- this honors an explicit
    git install rather than migrating it. An `editable` install is left
    alone entirely (dev checkout -- manage it via git yourself).

    Now that a real PyPI release exists, the "already up to date" version
    gate this module previously lacked (see AGENTS.md history) is real:
    unless `force=True`, an install already at the latest version/commit
    is reported and left untouched rather than reinstalled and restarted
    for no code change. Reporting style (plain `  ERROR: ...` on stderr
    equivalent) matches muxplex's.
    """
    from .service import service_install

    print("\nmuxplex-deck update\n")

    info = _get_install_info()
    commit_suffix = f" @ {info['commit'][:8]}" if info["commit"] else ""
    print(
        f"  Installed: muxplex-deck {info['version']} (via {info['source']}{commit_suffix})"
    )

    if info["source"] == "editable":
        print(
            "\n  Editable install detected -- manage updates via git yourself (no action taken).\n"
        )
        return

    if force:
        print("  Status: --force specified -- skipping version check")
    else:
        update_available, message = _check_for_update(info)
        print(f"  Status: {message}")
        if not update_available:
            print(
                "\n  Already up to date."
                " Use 'muxplex-deck update --force' to reinstall anyway.\n"
            )
            return

    install_target = "muxplex-deck" if info["source"] == "pypi" else f"git+{_REPO_URL}"

    was_active = _service_is_active()
    if was_active:
        print("  Stopping service...")
        if sys.platform == "darwin":
            import os

            uid = os.getuid()
            subprocess.run(
                ["launchctl", "bootout", f"gui/{uid}/com.muxplex-deck"],
                capture_output=True,
                check=False,
            )
        else:
            subprocess.run(
                ["systemctl", "--user", "stop", "muxplex-deck"],
                capture_output=True,
                check=False,
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
                [uv_path, "tool", "install", "--force", install_target],
                capture_output=True,
                text=True,
                check=False,
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
                    [pip_path, "install", "--upgrade", install_target],
                    capture_output=True,
                    text=True,
                    check=False,
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
            except Exception as exc:  # noqa: BLE001 -- report and continue; update must still finish
                print(f"  ERROR: service file regeneration failed: {exc}")
    finally:
        if was_active:
            print("  Restarting service...")
            try:
                from .service import service_restart

                service_restart()
            except Exception as exc:  # noqa: BLE001 -- report and continue; update must still finish
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
# wsl attach -- find, share-check, and attach the Stream Deck under WSL2.
#
# This is the ONLY command whose entire purpose is to mutate host state
# (WSL_COLD_START_SPEC.md section 6.1): it's the sole caller of
# `wsl.attach()`. Its deciding rationale isn't convenience -- it removes
# the `usbipd` vs `usbipd.exe` trap from the recurring path entirely (a
# user who never types either name cannot get the wrong binary; see
# section 6.4). Never invokes `sudo` (P3) -- when a permission fix is
# still needed after attaching, it prints the command, it doesn't run it.
# ---------------------------------------------------------------------------


# A settled-enough bound for sysfs to enumerate a just-attached device --
# generous enough to absorb the usbip attach -> udev/sysfs lag that
# produced a false W6 ("not visible yet") immediately followed by a
# `doctor` run that found the device fine, but tight enough that a
# genuinely-stuck attach still fails promptly.
_ATTACH_SETTLE_ATTEMPTS = 5
_ATTACH_SETTLE_DELAY_SECONDS = 0.2


def _find_usb_node_with_settle(usbnode_mod: Any, vendor_id: str) -> Any:
    """Poll for the USB node with a bounded settle window after an attach.

    A successful `wsl.attach()` can return before sysfs has finished
    enumerating the device on this side -- checking `find_usb_node` only
    once produced a false negative (W6: "attached but Linux doesn't see it
    yet") moments before the very next `doctor` run found the device
    working. Retries a few times with a short delay instead of declaring
    failure on the very first miss.
    """
    node = usbnode_mod.find_usb_node(vendor_id)
    for _ in range(_ATTACH_SETTLE_ATTEMPTS - 1):
        if node is not None:
            return node
        time.sleep(_ATTACH_SETTLE_DELAY_SECONDS)
        node = usbnode_mod.find_usb_node(vendor_id)
    return node


def wsl_attach(*, vendor_id: str = "0fd9") -> int:
    """`muxplex-deck wsl attach` -- attach the Stream Deck to this WSL2 distro.

    Exit 0 on a successful attach (even if a follow-up permission fix is
    still needed -- the attach itself succeeded). Exit 1 for every state
    that stops short of attaching (not WSL, WSL1, usbipd.exe missing, not
    found, not shared) so scripts can branch on it.
    """
    from . import hidhelp
    from . import usbnode as usbnode_mod
    from . import wsl as wsl_mod

    print("\nmuxplex-deck wsl attach\n")

    info = wsl_mod.detect()
    if not info.is_wsl:
        print_check(
            "fail", "Not running under WSL -- this command only makes sense there."
        )
        print()
        return 1
    print_check("ok", f"WSL{info.version} detected ({info.kernel})")

    if info.version == 1:
        print_check("fail", hidhelp.W1_MESSAGE)
        print()
        return 1

    paths = wsl_mod.find_usbipd()
    if paths.linux_impostor is not None and paths.windows is not None:
        print_check("warn", hidhelp.impostor_message(paths))

    if paths.windows is None:
        print_check("warn", hidhelp.W2_MESSAGE)
        print()
        return 1
    print_check("ok", f"usbipd.exe: {paths.windows}")

    devices = wsl_mod.list_devices(paths.windows, vendor_id=vendor_id)
    if devices is None:
        print_check("warn", hidhelp.W2_MESSAGE)
        print()
        return 1
    if not devices:
        print_check("warn", hidhelp.NO_MATCHING_WINDOWS_DEVICE_MESSAGE)
        print()
        return 1

    device = devices[0]
    print_check(
        "ok",
        f"Found on Windows: BUSID {device.busid}  {device.vid_pid}  {device.description}",
    )

    if device.state == "not_shared":
        print_check("warn", hidhelp.w4_message(device))
        print()
        return 1

    if device.state == "unknown":
        print_check("warn", hidhelp.w3_unknown_state_message(device))
        print()
        return 1

    if device.state == "shared":
        print_check("ok", "Shared -- attaching...")
        success, message = wsl_mod.attach(paths.windows, device.busid)
        if not success:
            print_check("fail", f"Attach failed: {message}")
            print()
            return 1
        print_check("ok", "Attached")
    else:
        print_check("ok", f"Already attached (BUSID {device.busid})")

    node = _find_usb_node_with_settle(usbnode_mod, vendor_id)
    if node is None:
        print_check("warn", hidhelp.w6_message(device))
        print()
        return 1
    print_check("ok", f"Visible to Linux: {node.path}")

    if not node.readable_writable:
        print_check("warn", hidhelp.w7_message(node))
        if not usbnode_mod.udev_is_live():
            print_check(
                "warn",
                hidhelp.u_dead_wsl_message(wsl_mod.wsl_conf_systemd_state()),
            )

    print()
    print("  Next:")
    print("    muxplex-deck service restart")
    print("    muxplex-deck status")
    print()
    print(
        "  The device number changes on every attach -- re-run "
        "`muxplex-deck wsl attach`\n  after any unplug or Windows reboot."
    )
    print()
    return 0


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

    status_parser = sub.add_parser(
        "status", help="Show connected hardware + connection state"
    )
    status_parser.add_argument(
        "--json", action="store_true", help="Emit raw status as JSON"
    )

    update_parser = sub.add_parser(
        "update",
        aliases=["upgrade"],
        help="Update muxplex-deck to the latest version and restart the service",
    )
    update_parser.add_argument(
        "--force",
        action="store_true",
        help="Reinstall even if already at the latest version/commit",
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

    wsl_parser = sub.add_parser("wsl", help="WSL2 USB/IP helpers")
    wsl_sub = wsl_parser.add_subparsers(dest="wsl_command")
    wsl_sub.add_parser(
        "attach",
        help="Find, share-check, and attach the Stream Deck to this WSL2 distro",
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
    elif args.command == "status":
        sys.exit(
            status(getattr(args, "config", None), as_json=getattr(args, "json", False))
        )
    elif args.command in ("update", "upgrade"):
        update(force=getattr(args, "force", False))
    elif args.command == "init":
        from .init_wizard import run_init

        sys.exit(
            run_init(
                getattr(args, "config", None),
                getattr(args, "server_url", None),
                non_interactive=getattr(args, "non_interactive", False),
            )
        )
    elif args.command == "wsl":
        cmd = getattr(args, "wsl_command", None)
        if cmd == "attach":
            sys.exit(wsl_attach())
        else:
            wsl_parser.print_help()
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
        from .service import (
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
