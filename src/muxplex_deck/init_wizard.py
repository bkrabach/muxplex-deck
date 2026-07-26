"""`muxplex-deck init` -- turnkey interactive setup wizard.

Closes the gap between "just installed" and "actually working" without
requiring SSH access to the muxplex server. muxplex's own CLI auto-derives
everything on first run (password, signing secret, device_id); this wizard
gives the sidecar the same experience for the two things it genuinely can't
invent (a server URL and a shared secret), while auto-fetching the CA
certificate wherever possible instead of requiring an `scp`.

Flow (see README.md "Quickstart" for the user-facing summary):

1. Server URL -- prompt (or take it as a positional arg / existing config).
2. Validate -- `GET /api/instance-info`. A TLS/cert failure is *expected*
   for a server on its own local CA and just means "keep going, we'll fetch
   or ask for the CA next" rather than a fatal error.
3. CA certificate -- try `GET /api/ca` (verify disabled for this one
   bootstrap fetch: a CA public cert is a trust anchor, not a secret) first;
   fall back to a validated local-path prompt only if that 404s AND TLS was
   actually failing; skip entirely if TLS already works without one.
4. Federation key -- reuse an existing valid key file if present, otherwise
   prompt to paste it (never echoed, never logged) or read it from a local
   path.
5. Write config.json via the existing `config.patch_raw_config` (merges,
   never clobbers unrelated keys).
6. Re-verify server reachability + deck detection with the new config in
   place, using the same `doctor`-style check functions and formatting.
7. Print udev remediation if needed, offer to run `service install`.

Every step reuses the pure check helpers already in `cli.py`
(`check_server_reachable`, `check_ca_file`, `check_federation_key`,
`check_deck_detected`, `check_hid_openable`) and `service.udev_rule_exists()`
rather than duplicating their logic -- this module only adds the
interactive orchestration and the two raw data-fetch primitives
(`cli.fetch_instance_info`, `cli.fetch_ca_cert`) that check_server_reachable
alone doesn't expose.
"""

from __future__ import annotations

import getpass
import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

from . import cli as cli_mod
from . import config as config_mod
from . import service as service_mod


class _InitError(Exception):
    """Raised internally to abort the wizard with a clear, already-final message."""


def _normalize_server_url(url: str) -> str:
    """Add `https://` if no scheme was given; strip a trailing slash."""
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    return url.rstrip("/")


def _sha256_fingerprint(cert_bytes: bytes) -> str:
    digest = hashlib.sha256(cert_bytes).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def _prompt(
    label: str, *, default: str | None, input_func: Callable[[str], str]
) -> str:
    """Prompt with an optional default shown in brackets; Enter keeps it."""
    if default:
        raw = input_func(f"{label} [{default}]: ")
    else:
        raw = input_func(f"{label}: ")
    raw = raw.strip()
    return raw if raw else (default or "")


def _confirm(label: str, *, default: bool, input_func: Callable[[str], str]) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    raw = input_func(f"{label} {suffix}: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")


# ---------------------------------------------------------------------------
# Step 2: validate the server (with a re-prompt loop on plain connection errors)
# ---------------------------------------------------------------------------


def _validate_server(
    server_url: str,
    *,
    non_interactive: bool,
    input_func: Callable[[str], str],
) -> tuple[str, dict | None, bool]:
    """Returns (possibly-updated server_url, instance_info or None, tls_needed)."""
    current = server_url
    while True:
        print(f"\nChecking {current} ...")
        try:
            data = cli_mod.fetch_instance_info(current, verify=True)
        except Exception as exc:
            if cli_mod._is_tls_error(exc):
                print(f"  ! TLS verification failed: {exc}")
                print(
                    "    This server may be using its own local CA -- continuing "
                    "to fetch/configure it next."
                )
                return current, None, True
            print(f"  Could not reach {current}: {exc}")
            if non_interactive:
                raise _InitError(f"server unreachable: {exc}") from exc
            current = _prompt("Server URL", default=current, input_func=input_func)
            if not current:
                raise _InitError("a server URL is required")
            current = _normalize_server_url(current)
            continue

        name = data.get("name", "?")
        version = data.get("version", "?")
        print(f"  Found muxplex '{name}' running v{version}")
        if data.get("federation_enabled") is False:
            print(
                "  ! Federation is not enabled on this server yet -- run "
                "`muxplex generate-federation-key` on the server first, then "
                "re-run this wizard."
            )
        return current, data, False


# ---------------------------------------------------------------------------
# Step 3: CA certificate
# ---------------------------------------------------------------------------


def _resolve_ca(
    server_url: str,
    *,
    existing_ca: str | None,
    tls_needed: bool,
    non_interactive: bool,
    input_func: Callable[[str], str],
) -> str | None:
    try:
        ca_bytes = cli_mod.fetch_ca_cert(server_url)
    except Exception as exc:  # noqa: BLE001 -- network/HTTP errors from the bootstrap fetch
        print(f"  ! Could not query {server_url}/api/ca: {exc}")
        ca_bytes = None

    if ca_bytes:
        ca_path = config_mod._expand("~/.config/muxplex-deck/muxplex-ca.crt")
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_bytes(ca_bytes)
        ca_path.chmod(0o644)
        fingerprint = _sha256_fingerprint(ca_bytes)
        print(f"  CA certificate fetched and saved: {ca_path}")
        print(f"  SHA-256 fingerprint: {fingerprint}")
        print(
            "  Verify out-of-band on the server if you'd like:\n"
            "    openssl x509 -in ~/.config/muxplex/ca/muxplex-ca.crt "
            "-noout -fingerprint -sha256"
        )
        return str(ca_path)

    if not tls_needed:
        print("  CA: not needed (server certificate verifies without one)")
        return None

    print(
        "  ! No CA available at /api/ca, but TLS verification failed -- "
        "this server needs a CA file and we can't self-serve it."
    )
    if non_interactive:
        raise _InitError(
            "TLS verification failed and no CA certificate could be "
            "auto-fetched. Provide 'ca_file' in config.json, or re-run "
            "`muxplex-deck init` interactively."
        )

    default_path = existing_ca
    while True:
        entered = _prompt(
            "Path to the server's CA certificate file",
            default=default_path,
            input_func=input_func,
        )
        if not entered:
            raise _InitError(
                "a CA certificate path is required (TLS verification is failing)"
            )
        candidate = config_mod._expand(entered)
        status, message = cli_mod.check_ca_file(candidate)
        if status == "ok":
            return str(candidate)
        print(f"  ! {message}")
        default_path = entered


# ---------------------------------------------------------------------------
# Step 4: federation key -- never printed, never logged
# ---------------------------------------------------------------------------


def _resolve_federation_key(
    key_file_path: Path,
    *,
    non_interactive: bool,
    input_func: Callable[[str], str],
    getpass_func: Callable[[str], str],
) -> None:
    if key_file_path.exists() and key_file_path.stat().st_size > 0:
        if non_interactive:
            print(f"  Federation key: keeping existing {key_file_path}")
            return
        if _confirm(
            f"Federation key already exists at {key_file_path}. Keep it?",
            default=True,
            input_func=input_func,
        ):
            print(f"  Federation key: keeping existing {key_file_path}")
            return

    if non_interactive:
        raise _InitError(
            f"federation key not found at {key_file_path}. Paste it "
            "interactively once (re-run without --non-interactive), or "
            f"copy it there yourself first, e.g.:\n"
            f"  scp <your-server>:.config/muxplex/federation_key {key_file_path}"
        )

    print("  Paste the federation key, or enter a local file path containing it.")
    print(
        "  Get it from the server with `muxplex generate-federation-key`, or "
        "read ~/.config/muxplex/federation_key there. SSH fallback:"
    )
    print(f"    scp <your-server>:.config/muxplex/federation_key {key_file_path}")
    pasted = getpass_func("Federation key (or path): ")
    value = pasted.strip()
    candidate_path = Path(value).expanduser() if value else None

    if candidate_path is not None and candidate_path.is_file():
        key_value = candidate_path.read_text(encoding="utf-8").strip()
    else:
        key_value = value

    if not key_value:
        raise _InitError("no federation key provided")

    key_file_path.parent.mkdir(parents=True, exist_ok=True)
    key_file_path.parent.chmod(0o700)
    key_file_path.write_text(key_value + "\n", encoding="utf-8")
    key_file_path.chmod(0o600)
    print(f"  Federation key written: {key_file_path} (mode 0600)")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _run_init_impl(
    config_path: str | None,
    server_url_arg: str | None,
    *,
    non_interactive: bool,
    input_func: Callable[[str], str],
    getpass_func: Callable[[str], str],
) -> int:
    raw = config_mod.load_raw_config(config_path)

    print("\nmuxplex-deck init\n")

    # Step 1: server URL
    existing_url = raw.get("server_url") or None
    if server_url_arg:
        server_url = server_url_arg
    elif non_interactive:
        if not existing_url:
            raise _InitError(
                "SERVER_URL is required in --non-interactive mode, e.g.:\n"
                "  muxplex-deck init https://your-server:8088 --non-interactive"
            )
        server_url = existing_url
    else:
        entered = _prompt("Server URL", default=existing_url, input_func=input_func)
        if not entered:
            raise _InitError("a server URL is required")
        server_url = entered
    server_url = _normalize_server_url(server_url)

    # Step 2: validate
    server_url, _instance_data, tls_needed = _validate_server(
        server_url, non_interactive=non_interactive, input_func=input_func
    )

    # Step 3: CA certificate
    print()
    ca_file_value = _resolve_ca(
        server_url,
        existing_ca=raw.get("ca_file") or None,
        tls_needed=tls_needed,
        non_interactive=non_interactive,
        input_func=input_func,
    )

    # Step 4: federation key
    print()
    key_file_raw = raw.get("key_file") or config_mod.DEFAULT_KEY_FILE
    _resolve_federation_key(
        config_mod._expand(key_file_raw),
        non_interactive=non_interactive,
        input_func=input_func,
        getpass_func=getpass_func,
    )

    # Step 5: write config (merge -- preserves sort/poll_interval/focus_app/etc.)
    patch: dict = {"server_url": server_url, "key_file": key_file_raw}
    if ca_file_value:
        patch["ca_file"] = ca_file_value
    config_mod.patch_raw_config(patch, config_path)
    resolved_path = config_mod._resolve_config_path(config_path)
    print(f"\nConfig saved: {resolved_path}")

    # Step 6: verify
    print("\nVerifying...")
    final_raw = config_mod.load_raw_config(config_path)
    ca_path = (
        config_mod._expand(final_raw["ca_file"]) if final_raw.get("ca_file") else None
    )
    status, message = cli_mod.check_server_reachable(server_url, ca_path)
    cli_mod.print_check(status, message)
    status, message = cli_mod.check_deck_detected(config_path)
    cli_mod.print_check(status, message)
    status, message = cli_mod.check_hid_openable()
    cli_mod.print_check(status, message)

    # Step 7: next steps
    if sys.platform not in ("darwin", "win32") and not service_mod.udev_rule_exists():
        print()
        print(service_mod._UDEV_REMEDIATION)

    if not non_interactive and _confirm(
        "\nRun `muxplex-deck service install` now?",
        default=False,
        input_func=input_func,
    ):
        service_mod.service_install()

    print("\nYou're set up -- run `muxplex-deck` or `muxplex-deck service install`.\n")
    return 0


def run_init(
    config_path: str | None,
    server_url_arg: str | None = None,
    *,
    non_interactive: bool = False,
    input_func: Callable[[str], str] = input,
    getpass_func: Callable[[str], str] = getpass.getpass,
) -> int:
    """Run the interactive setup wizard. Idempotent and safe to re-run.

    Returns a process exit code (0 success, 1 a required input/validation
    failed, 130 aborted via Ctrl-C/EOF before any config was written).
    """
    try:
        return _run_init_impl(
            config_path,
            server_url_arg,
            non_interactive=non_interactive,
            input_func=input_func,
            getpass_func=getpass_func,
        )
    except _InitError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n\nAborted -- no changes made.", file=sys.stderr)
        return 130
