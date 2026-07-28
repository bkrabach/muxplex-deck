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
import os
import platform
import shutil
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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Write logs to this file instead of stderr (default: stderr, unchanged "
        "behavior). All platforms -- required in practice under Windows Task "
        "Scheduler, since pythonw.exe has no console for stderr to go to "
        "(see WINDOWS_NATIVE_SPEC.md section 1.5); `muxplex-deck service install` "
        "passes this automatically on Windows.",
    )


def run(
    config_path: str | None = None,
    *,
    emulator: bool = False,
    emulator_port: int = 8484,
    log_file: str | None = None,
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

    log_path = Path(log_file).expanduser() if log_file else None
    return main_mod.run(cfg, manager, log_file=log_path)


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
    """Federation key presence + permission check. Never prints the key itself.

    On Windows, `Path.chmod()` only toggles the read-only bit -- it does not
    restrict other users -- so `st_mode` reads back as a permissive value
    (typically 0o666) no matter what actually protects the file (NTFS ACLs,
    which are per-user profile directories by default). Warning on every
    run and recommending a `chmod` that cannot fix anything would violate
    this repo's own rule (AGENTS.md: never print a command that cannot work
    on the machine you are printing it to) -- so Windows gets presence-only
    reporting, no POSIX-mode judgment, and no ACL check (that would be new,
    untested, platform-specific security code for a file already inside a
    per-user profile directory; see WINDOWS_NATIVE_SPEC.md section 3.3).
    """
    if not key_file.exists():
        return "warn", f"Federation key file not found: {key_file}"
    try:
        mode = key_file.stat().st_mode & 0o777
    except OSError as exc:
        return "warn", f"Could not stat federation key file {key_file}: {exc}"
    if sys.platform == "win32":
        return "ok", (
            f"Federation key: {key_file} "
            "(NTFS ACLs govern access here -- POSIX modes are not enforced)"
        )
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
        if sys.platform == "win32":
            # Windows doesn't ship openssl by default -- unlike Linux/macOS,
            # where its absence is unusual and worth a "warn", here it's the
            # norm. Flagging this as something the user needs to fix would
            # violate the repo's own rule (never print guidance that treats
            # an environment gap as the user's problem when it isn't one).
            # `ok`, with an actionable-if-they-want-it alternative -- never
            # invent an X.509 parser dependency for this one check.
            return "ok", (
                "ca_file: could not verify via openssl (not bundled with "
                "Windows) -- compare the SHA-256 fingerprint printed "
                "during `init` against the server's, or install openssl "
                "(winget install ShiningLight.OpenSSL.Light, or use Git "
                "for Windows' bundled openssl.exe) to enable this check"
            )
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


def check_hidapi_dll() -> tuple[str, str] | None:
    """Windows-only: report which `hidapi.dll` streamdeck's loader will actually use.

    Returns `None` on every other platform (nothing to check -- other OSes
    use their own dynamic linker conventions and the check is simply
    skipped, not appended to the doctor output).

    `hidapi_win.ensure_hidapi()` can create a silent shadowing failure mode
    if something earlier on `%PATH%` wins the load race (see that module's
    docstring for the mechanism) -- a design that creates a failure mode
    owes the user a way to see it, so this always prints a line on
    Windows, not only when something is wrong (WINDOWS_NATIVE_SPEC.md
    section 2.6).
    """
    if sys.platform != "win32":
        return None

    from . import hidapi_win

    dll_dir = hidapi_win.ensure_hidapi()
    if dll_dir is None:
        return "warn", (
            "hidapi.dll: vendored copy not found in this install (Windows "
            "arm64, or a source checkout without the vendored binary) -- "
            "see https://github.com/libusb/hidapi/releases"
        )

    vendored = str(hidapi_win.vendored_dll_path())
    resolved = hidapi_win.resolved_library_path()
    if resolved is None:
        return "warn", (
            f"hidapi.dll: vendored copy present at {vendored} but "
            "ctypes.util.find_library('hidapi') did not resolve anything "
            "(check %PATH%)"
        )
    # Case-insensitive: see hidhelp._windows_guidance()'s comment on the
    # same comparison -- os.path.normcase only behaves case-insensitively
    # when the running process actually IS Windows, not when sys.platform
    # is merely monkeypatched (as tests, and this repo's Linux-only CI, do).
    if resolved.lower() != vendored.lower():
        return "warn", (
            f"hidapi.dll: resolves to {resolved} -- NOT the vendored copy "
            "(see the WIN-DLL-SHADOW guidance below)"
        )
    return "ok", f"hidapi.dll: resolves to the vendored copy ({resolved})"


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


def check_federation_key_auth(
    server_url: str, federation_key: str, *, verify: bool | str = True
) -> tuple[str, str]:
    """Issue ONE authenticated request to confirm `federation_key` is actually accepted.

    `/api/instance-info` (what `check_server_reachable`/`fetch_instance_info`
    call) is unauthenticated by design on the server -- it proves TLS and
    reachability, but nothing about a credential. That gap is exactly what
    let `init` once print a green "Server reachable" check, write config,
    and offer to install the service for a federation key that was simply
    wrong -- the user only found out once the running service couldn't
    authenticate (see AGENTS.md).

    This hits `GET /api/sessions` instead: authenticated, read-only, the
    same lightweight endpoint the sidecar's own active loop already polls
    (see `main.py`), with the key sent as `Authorization: Bearer` -- the
    exact scheme the muxplex server's `auth.py` checks. Returns one of:

      ("ok", ...)      -- HTTP 200: the key is accepted.
      ("fail", ...)     -- HTTP 401: the key is definitively rejected.
                           Callers must not save a key that gets this back.
      ("warn", ...)     -- anything else (timeout, connection error,
                           unexpected status): could not verify one way or
                           the other. This is NOT evidence the key is bad --
                           callers must not print a false success, but also
                           must not treat an unrelated network hiccup as a
                           rejected key.

    **Must send `Accept: application/json`.** This is the root cause of a
    real regression: without it, muxplex's auth middleware treats the
    request as a browser navigation and answers an unauthenticated
    request with a 307 redirect to `/login` instead of a 401 -- which this
    client (deliberately, like `muxplex_client.MuxplexClient`, see its
    docstring) does not follow, so the redirect surfaced as an
    "unexpected response HTTP 307" that read as "can't verify" and let a
    bad key through unvalidated. `follow_redirects` is NOT the fix --
    following the redirect would land on the login page and return a
    misleading 200. Sending the header the server's auth layer actually
    branches on is what makes this request exercise the credential at
    all, exactly like the sidecar's own `MuxplexClient` already does.
    """
    import httpx

    url = f"{server_url.rstrip('/')}/api/sessions"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {federation_key}",
    }
    try:
        with httpx.Client(verify=verify, timeout=5.0) as client:
            resp = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        return (
            "warn",
            f"Could not verify federation key -- server check timed out: {exc}",
        )
    except httpx.ConnectError as exc:
        return "warn", f"Could not verify federation key -- server unreachable: {exc}"
    except Exception as exc:  # noqa: BLE001 -- verification must degrade to warn, never raise
        return "warn", f"Could not verify federation key: {exc}"

    if resp.status_code == 200:
        return "ok", "Federation key accepted by server"
    if resp.status_code == 401:
        return "fail", "Federation key rejected by server (401 Unauthorized)"
    return "warn", (
        f"Could not verify federation key -- unexpected response HTTP {resp.status_code}"
    )


def check_service_status() -> tuple[str, str]:
    """Service-state check: not installed / installed but not running / running.

    Three distinct states, not two -- `service_is_installed()` (unit/plist
    file exists) and `service_is_active()` (currently running) are checked
    independently, so an installed-but-crash-looping service is reported
    honestly instead of as "not installed" (which used to tell the user to
    re-run `service install` for a service that was already installed and
    failing -- see AGENTS.md for the incident this fixes).
    """
    from .service import service_is_active, service_is_installed

    if sys.platform == "darwin":
        manager, tool = "launchd", "launchctl"
    elif sys.platform == "win32":
        # Task Scheduler has no single "is the tool present" probe the way
        # `shutil.which("systemctl")` does -- `schtasks.exe` ships with
        # every Windows install, so there's nothing to gate on here; skip
        # straight to the installed/active checks below.
        manager, tool = "Task Scheduler", None
    else:
        manager, tool = "systemd", "systemctl"

    if tool is not None and shutil.which(tool) is None:
        return "warn", f"Service: {tool} not found -- run muxplex-deck directly"

    if not service_is_installed():
        return "warn", "Service: not installed -- run: muxplex-deck service install"

    if service_is_active():
        return "ok", f"Service: installed and running ({manager})"

    return "warn", (
        f"Service: installed ({manager}) but not running -- check: "
        "muxplex-deck service logs"
    )


_CHECK_MARKS: dict[str, str] = {
    "ok": "\033[32m\u2713\033[0m",
    "fail": "\033[31m\u2717\033[0m",
    "warn": "\033[33m!\033[0m",
    # Distinct from "warn": "warn" means a check RAN and found something to
    # flag; "unknown" means the check could not be run at all yet (e.g. the
    # currently-running process hasn't published a status write of its own
    # yet). Conflating the two is what let a dying process's stale-but-
    # recent status snapshot get reported as definite failures right after
    # `service restart` -- see AGENTS.md's restart-race incident.
    "unknown": "\033[36m?\033[0m",
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


def _status_glyph(status: str) -> str:
    """Map a check helper's ("ok"|"warn"|"fail") status to a report glyph.

    "ok" -> fine. "fail" (a definitive rejection) and "warn" (almost always
    paired with a "run: ..." remediation already in the message) both ->
    act-now. Checks that are genuinely blocked on an upstream item (e.g.
    server/service before config exists) are never routed through this
    mapper -- callers construct those `Check`s directly with
    `report.BLOCKED` instead.
    """
    from . import report

    return report.FINE if status == "ok" else report.ACT


def _hid_tail(hid_message: str) -> str:
    """Short phrase summarizing a successful HID-open, for the merged device line."""
    text = hid_message
    text = text.removeprefix("HID: ")
    if text.startswith("device opened successfully"):
        return "HID opens"
    if text.startswith("device in use by the muxplex-deck service"):
        return "HID in use by service"
    return text if text.startswith("HID") else f"HID: {text}"


def _device_hid_checks(
    deck_status: str, deck_message: str, hid_status: str, hid_message: str
) -> list[Any]:
    """Merge device-detection + HID-open into one line when they agree.

    When both share a glyph there's nothing to hide by combining them --
    one readable "device" line beats two boring ones. When they diverge
    (e.g. the device is visible on Windows but WSL can't open it yet),
    collapsing to a single worst-member line would silently drop whichever
    fact ACTION needs to reference (like a busid) -- so they render as two
    independent, dependency-ordered lines instead: "device" (is it
    physically present) then "hid" (can this process open it).
    """
    from . import report

    deck_glyph = _status_glyph(deck_status)
    hid_glyph = _status_glyph(hid_status)
    if deck_glyph == hid_glyph:
        tail = _hid_tail(hid_message)
        value = f"{deck_message}, {tail}" if tail else deck_message
        return [report.Check("device", deck_glyph, value)]
    return [
        report.Check("device", deck_glyph, deck_message),
        report.Check("hid", hid_glyph, hid_message),
    ]


def _doctor_decision(check: Any) -> Any:
    """Build the ACTION decision for the first act-now check, in dependency order."""
    from . import report

    if check.subject == "config":
        return report.Decision(
            commands=["muxplex-deck init"],
            prose=(
                "Creates the config, fetches and fingerprints the server CA, "
                "stores the federation key, and offers to install the service."
            ),
        )
    command = report.extract_run_command(check.value)
    if command:
        return report.Decision(commands=[command])
    return report.Decision(commands=[], prose=check.value)


def _build_doctor_action(collapsed: list[Any]) -> list[str] | None:
    """The ACTION band: the first act-now item's decision, plus an overflow

    note when more than one independent act-now item exists (see
    report.Action.overflow_note) -- never dump every fix at once.
    """
    from . import report

    act_items = [c for c in collapsed if c.glyph == report.ACT]
    if not act_items:
        return None
    overflow = None
    if len(act_items) > 1:
        overflow = f"{len(act_items) - 1} more after this -- rerun doctor."
    action = report.Action(
        decision=_doctor_decision(act_items[0]), overflow_note=overflow
    )
    return report.render_action(action)


def doctor(config_path: str | None = None, *, show_all: bool = False) -> int:
    """Run diagnostic checks and report system status. Always returns 0 (informational)."""
    from . import hidhelp, report

    utf8 = report.utf8_capable()

    py_status, py_message = check_python_version()
    inst_status, inst_message = check_install_and_update()
    env_members = [
        report.Check("python", _status_glyph(py_status), py_message),
        report.Check("install", _status_glyph(inst_status), inst_message),
    ]
    hidapi_check = check_hidapi_dll()  # Windows-only; None everywhere else.
    if hidapi_check is not None:
        hidapi_status, hidapi_message = hidapi_check
        env_members.append(
            report.Check("hidapi", _status_glyph(hidapi_status), hidapi_message)
        )
    environment_group = report.Group("environment", env_members)

    cfg_status, cfg_message = check_config_file(config_path)
    try:
        cfg = config_mod.load_config(config_path)
    except ConfigError:
        cfg = None

    raw = config_mod.load_raw_config(config_path)
    key_file = config_mod._expand(raw.get("key_file", config_mod.DEFAULT_KEY_FILE))
    key_status, key_message = check_federation_key(key_file)

    ca_file = cfg.ca_file if cfg is not None else None
    if cfg is None and raw.get("ca_file"):
        ca_file = config_mod._expand(raw["ca_file"])
    ca_status, ca_message = check_ca_file(ca_file)

    config_group = report.Group(
        "config",
        [
            report.Check("file", _status_glyph(cfg_status), cfg_message),
            report.Check("key", _status_glyph(key_status), key_message),
            report.Check("ca", _status_glyph(ca_status), ca_message),
        ],
    )
    config_created = cfg_status == "ok"

    items: list[Any] = [environment_group, config_group]

    # Environment guidance (WSL/usbipd/udev-liveness) BEFORE the device
    # checks -- it explains why the next line is about to warn. Returns []
    # on a healthy platform (macOS, or native Linux with udev running), so
    # this adds no output there -- see hidhelp.explain_environment().
    env_guidances = hidhelp.explain_environment()
    for guidance in env_guidances:
        items.append(
            report.Check("device", _status_glyph(guidance.status), guidance.message)
        )
    env_states = {g.state for g in env_guidances}

    if not config_created:
        # Server/service structurally need server_url/config -- blocked,
        # not independently evaluated. Device/HID don't depend on config
        # at all (a bare USB probe works with no config file), so they are
        # still evaluated for real below.
        items.append(report.Check("server", report.BLOCKED, "waiting on config"))
        items.append(report.Check("service", report.BLOCKED, "waiting on config"))

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
            deck_message = "not detected on this OS yet -- see the WSL guidance above."
        hid_status, hid_message = check_hid_openable()
        items.extend(
            _device_hid_checks(deck_status, deck_message, hid_status, hid_message)
        )

    if config_created:
        server_url = cfg.server_url if cfg is not None else raw.get("server_url", "")
        srv_status, srv_message = check_server_reachable(server_url, ca_file)
        items.append(report.Check("server", _status_glyph(srv_status), srv_message))

        svc_status, svc_message = check_service_status()
        items.append(report.Check("service", _status_glyph(svc_status), svc_message))

    collapsed = report.collapsed_checks(items)
    action_count = report.count_actions([c.glyph for c in collapsed])
    verdict = report.verdict_readiness(action_count)
    state_lines = report.render_items(items, show_all=show_all, utf8=utf8)
    action_lines = _build_doctor_action(collapsed)

    sys.stdout.write(report.render(verdict, state_lines, action_lines))
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


def _status_device_item(device: dict[str, Any]) -> Any:
    """Build the "device" Check from the sidecar's published status snapshot.

    "-" is the correct rendering for "I could not determine this" -- during
    a restart race the caller returns before this is ever called (see the
    pid-freshness guard in `status()`), so this function only ever runs
    when the snapshot is genuinely current.
    """
    from . import report

    if not device.get("connected"):
        return report.Check("device", report.ACT, "not connected")
    caps = device.get("capabilities") or {}
    hint = device.get("hint")
    if not caps:
        glyph = report.ACT if hint else report.FINE
        return report.Check("device", glyph, "connected (capabilities unavailable)")
    touchscreen = "yes" if caps.get("has_touchscreen") else "no"
    value = (
        f"{caps.get('model', '?')} -- {caps.get('key_count', '?')} keys "
        f"({caps.get('key_rows', '?')}x{caps.get('key_cols', '?')}), "
        f"{caps.get('dial_count', '?')} dials, touchscreen={touchscreen}"
    )
    return report.Check("device", report.FINE, value)


def _status_server_item(server: dict[str, Any]) -> Any:
    from . import report

    url = server.get("url") or "(not configured)"
    if server.get("connected"):
        return report.Check("server", report.FINE, f"{url} (reachable)")
    err = server.get("last_error")
    value = f"{url} (unreachable)"
    if err:
        value += f" -- {err}"
    return report.Check("server", report.ACT, value)


def _status_view_value(state: dict[str, Any]) -> str:
    view_name = state.get("active_view") or "all"
    page = state.get("page")
    return f"{view_name} (page {page})" if page is not None else view_name


def _direct_probe_item(config_path: str | None) -> Any:
    """Fallback when the service isn't running -- nothing holds the device."""
    from . import report

    deck_status, deck_message = check_deck_detected(config_path)
    return report.Check("device", _status_glyph(deck_status), deck_message)


def status(
    config_path: str | None = None, *, as_json: bool = False, show_all: bool = False
) -> int:
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
    from . import report
    from .service import service_is_active, service_main_pid
    from .statusfile import read_status

    running = service_is_active()
    data = read_status()

    if as_json:
        payload: dict[str, Any] = {"service_running": running, "status": data}
        print(json.dumps(payload, indent=2))
        return 0 if data is not None else 1

    utf8 = report.utf8_capable()

    if not running:
        probe_note = (
            "no status file -- probing the device directly instead (safe: "
            "nothing holds it while the service is stopped)"
            if data is None
            else "not trusting the (possibly stale) status file -- probing "
            "the device directly instead"
        )
        items: list[Any] = [
            report.Check("service", report.BLOCKED, "not running"),
            report.Check("probe", report.BLOCKED, probe_note),
            _direct_probe_item(config_path),
        ]
        collapsed = report.collapsed_checks(items)
        action_count = report.count_actions([c.glyph for c in collapsed])
        verdict = (
            "Not connected -- service not running."
            if action_count == 0
            else report.verdict_readiness(action_count)
        )
        state_lines = report.render_items(items, show_all=show_all, utf8=utf8)
        sys.stdout.write(report.render(verdict, state_lines, None))
        return 0

    if data is None:
        items = [
            report.Check(
                "status",
                report.BLOCKED,
                "no status file yet -- the sidecar may have just started. "
                "Try: muxplex-deck service logs",
            )
        ]
        state_lines = report.render_items(items, show_all=show_all, utf8=utf8)
        sys.stdout.write(
            report.render("Not connected -- starting up.", state_lines, None)
        )
        return 0

    # Is this snapshot from the process running RIGHT NOW, or from a
    # PREVIOUS incarnation? Age alone can't tell: a process that's about to
    # be replaced (e.g. by `service restart`) can write its LAST status
    # moments before exiting, so it still looks "fresh" by age even though
    # it no longer reflects reality -- exactly the restart-race incident in
    # AGENTS.md (2 false failures reported from a dying process's stale-but-
    # recent write). Comparing the recorded pid to the service manager's own
    # live MainPID is the reliable signal; fall back to the age check only
    # when the live pid can't be determined at all (unsupported platform,
    # command failure).
    current_pid = service_main_pid()
    recorded_pid = data.get("pid")
    if current_pid is not None:
        is_current = recorded_pid == current_pid
    else:
        is_current = (
            time.time() - data.get("updated_at", 0)
        ) <= _STATUS_STALE_THRESHOLD_SECONDS

    if not is_current:
        # "-" (BLOCKED), never "!" (act-now): we did not actually observe
        # this instant, so nothing here has earned a verdict yet -- see the
        # restart-race incident this guards against (AGENTS.md).
        items = [
            report.Check(
                "status",
                report.BLOCKED,
                f"not yet available for the running process (pid "
                f"{current_pid if current_pid is not None else '?'}) -- the "
                f"published status is from a previous run (pid {recorded_pid}). "
                "This is expected right after (re)starting; try again in a "
                "moment, or: muxplex-deck service logs",
            )
        ]
        state_lines = report.render_items(items, show_all=show_all, utf8=utf8)
        sys.stdout.write(
            report.render("Not connected -- previous run's data.", state_lines, None)
        )
        return 0

    age = time.time() - data.get("updated_at", 0)
    stale = age > _STATUS_STALE_THRESHOLD_SECONDS
    device_item = _status_device_item(data.get("device", {}))
    server_item = _status_server_item(data.get("server", {}))
    state = data.get("state", {})

    session_readout = report.Readout("session", state.get("active_session") or "none")
    view_readout = report.Readout("view", _status_view_value(state))
    pid_readout = report.Readout(
        "pid", f"{data.get('pid', '?')} (updated {age:.0f}s ago)"
    )

    all_fine = (
        not stale
        and device_item.glyph == report.FINE
        and server_item.glyph == report.FINE
    )

    if all_fine:
        readouts = [
            session_readout,
            view_readout,
            report.Readout("device", device_item.value),
            report.Readout("server", server_item.value),
            pid_readout,
        ]
        state_lines = report.render_readouts(readouts)
        sys.stdout.write(report.render("Running.", state_lines, None))
        return 0

    lines: list[str] = [
        report.format_readout_line(session_readout.name, session_readout.value),
        report.format_readout_line(view_readout.name, view_readout.value),
        report.format_check_line(
            device_item.glyph, device_item.subject, device_item.value, utf8=utf8
        ),
    ]
    hint = data.get("device", {}).get("hint")
    if hint and device_item.glyph != report.FINE:
        # Populated by the sidecar's open-failure branch (see main.py /
        # hidhelp.explain_open_failure) -- this is what turns a stale
        # status file into the primary teaching surface instead of a
        # bare "not connected" with no explanation.
        lines.append(report.format_check_line(report.ACT, "hint", hint, utf8=utf8))
    lines.append(
        report.format_check_line(
            server_item.glyph, server_item.subject, server_item.value, utf8=utf8
        )
    )
    if stale:
        lines.append(
            report.format_check_line(
                report.ACT,
                "status",
                f"stale (last updated {age:.0f}s ago) -- the sidecar may be "
                "stuck. Try: muxplex-deck service logs",
                utf8=utf8,
            )
        )
    lines.append(report.format_readout_line(pid_readout.name, pid_readout.value))

    glyphs = [device_item.glyph, server_item.glyph]
    if hint and device_item.glyph != report.FINE:
        glyphs.append(report.ACT)
    if stale:
        glyphs.append(report.ACT)
    verdict = report.verdict_readiness(report.count_actions(glyphs))
    sys.stdout.write(report.render(verdict, lines, None))
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
        # Was a hardcoded launchctl/systemctl-only block -- the `else`
        # branch ran `systemctl` unconditionally, which raises
        # `FileNotFoundError` on Windows (`check=False` only suppresses a
        # nonzero *exit*, not a failed *exec*). This was latent only
        # because `service_is_active()` returned False on Windows before
        # Task Scheduler support existed -- now that it can return True
        # there, this must dispatch per platform. `service.service_stop()`
        # already does exactly that (darwin/windows/systemd), so this is
        # strictly less code, one dispatch site, not a duplicated stop
        # implementation.
        from .service import service_stop

        service_stop()
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

    doctor_parser = sub.add_parser(
        "doctor", help="Check dependencies and system status"
    )
    doctor_parser.add_argument(
        "--all",
        "--verbose",
        dest="show_all",
        action="store_true",
        help="Show every underlying check (VID:PID, full paths, etc.) instead "
        "of the collapsed summary -- a strict superset, never a different answer",
    )

    status_parser = sub.add_parser(
        "status", help="Show connected hardware + connection state"
    )
    status_parser.add_argument(
        "--json", action="store_true", help="Emit raw status as JSON"
    )
    status_parser.add_argument(
        "--all",
        "--verbose",
        dest="show_all",
        action="store_true",
        help="Show every underlying check instead of the collapsed summary",
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
        doctor(getattr(args, "config", None), show_all=getattr(args, "show_all", False))
    elif args.command == "status":
        sys.exit(
            status(
                getattr(args, "config", None),
                as_json=getattr(args, "json", False),
                show_all=getattr(args, "show_all", False),
            )
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
