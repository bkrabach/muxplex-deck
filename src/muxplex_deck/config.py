"""Configuration loading for the muxplex-deck sidecar.

Config is a JSON file (default ``~/.config/muxplex-deck/config.json``,
overridable via the ``--config`` CLI flag or the ``MUXPLEX_DECK_CONFIG``
env var) plus a federation-key file referenced from it. All paths support
``~``, expanded relative to the *invoking* user's home: the sidecar is
typically launched as ``sudo muxplex-deck`` (HID access), and under sudo a
plain ``expanduser()`` would resolve to ``/root`` -- so ``~`` honors
``SUDO_USER`` when present (see `_expand`).

Any missing/invalid config or unreadable key file is a fail-loud, actionable
error -- there is no default that silently skips auth or TLS verification.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import pwd
except ImportError:  # non-POSIX (Windows) -- no sudo there, plain expanduser is fine
    pwd = None  # type: ignore[assignment]

DEFAULT_CONFIG_PATH = "~/.config/muxplex-deck/config.json"
DEFAULT_KEY_FILE = "~/.config/muxplex-deck/federation_key"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_SORT_MODE = "attention"
VALID_SORT_MODES = ("attention", "server")

# The full set of keys `config.json` supports, with their default values --
# drives the `muxplex-deck config` CLI group (list/get/set/reset) the same
# way muxplex's own DEFAULT_SETTINGS drives its `config` group. Unlike
# muxplex's server settings, `server_url` has no real default (it's a
# required field per `load_config`) -- "" here means "not configured".
DEFAULT_CONFIG: dict = {
    "server_url": "",
    "key_file": DEFAULT_KEY_FILE,
    "ca_file": "",
    "poll_interval": DEFAULT_POLL_INTERVAL_SECONDS,
    "sort": DEFAULT_SORT_MODE,
    "focus_app": "",
}


class ConfigError(Exception):
    """Raised for any config problem. The message is written to stderr as-is."""


def _invoking_user_home() -> Path:
    """Home directory of the user who launched the process, even under sudo.

    Under ``sudo``, ``$HOME``/``Path.home()`` resolve to ``/root`` -- but the
    config and key files live in the *invoking* user's home. When ``SUDO_USER``
    names a non-root user, resolve that user's home via the passwd database;
    otherwise fall back to the normal home.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if pwd is not None and sudo_user and sudo_user != "root":
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except KeyError:
            pass  # unknown user -- fall through to the normal home
    return Path.home()


def _expand(path: str | Path) -> Path:
    """Expand ``~`` relative to the invoking user's home (sudo-aware).

    Non-tilde paths pass through unchanged (modulo ``expanduser`` for the
    ``~otheruser`` form, which keeps its standard behavior).
    """
    text = str(path)
    if text == "~":
        return _invoking_user_home()
    if text.startswith("~/") or text.startswith("~\\"):
        return _invoking_user_home() / text[2:]
    return Path(text).expanduser()


@dataclass(frozen=True)
class Config:
    """Validated sidecar configuration, ready to hand to `MuxplexClient`."""

    server_url: str
    federation_key: str
    ca_file: Path | None
    poll_interval: float
    sort: str
    """"attention" (default): needs-attention sessions first, then the active
    session, then everything else by recent activity. "server": exactly the
    pre-existing behavior -- honor muxplex's own `sort_order` (alphabetical
    vs server/manual order) with no client-side reordering. See `.attention`
    for the "attention" mode's tie-break rules.
    """
    focus_app: str
    """macOS application name of the locally installed muxplex PWA to bring
    to the foreground when a key press switches the active session (see
    `.focus`). Empty (the default) disables the feature entirely -- no
    subprocess calls, no log noise. macOS-only today; on other platforms a
    configured value logs one INFO notice and is otherwise ignored.
    """


def _resolve_config_path(explicit: str | None) -> Path:
    if explicit:
        return _expand(explicit)
    env_value = os.environ.get("MUXPLEX_DECK_CONFIG")
    if env_value:
        return _expand(env_value)
    return _expand(DEFAULT_CONFIG_PATH)


def _load_federation_key(key_file: Path) -> str:
    if not key_file.exists():
        raise ConfigError(
            f"Federation key file not found: {key_file}\n"
            "Copy it from the muxplex server, e.g.:\n"
            f"  mkdir -p {key_file.parent}\n"
            f"  scp <your-server>:.config/muxplex/federation_key {key_file}\n"
            f"  chmod 600 {key_file}"
        )
    try:
        key = key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(
            f"Could not read federation key file {key_file}: {exc}"
        ) from exc
    if not key:
        raise ConfigError(f"Federation key file {key_file} is empty")
    return key


def load_config(config_path: str | None = None) -> Config:
    """Load and validate configuration.

    Raises `ConfigError` (with a message ready to print to stderr) for any
    problem: missing/invalid config file, missing required fields, missing
    or unreadable key file, or a `ca_file` that doesn't exist.
    """
    path = _resolve_config_path(config_path)

    if not path.exists():
        raise ConfigError(
            f"Config file not found: {path}\n"
            "Create it with at least a 'server_url' field, e.g.:\n"
            '  {"server_url": "https://<your-server>:8088"}\n'
            "See README.md for the full example."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            f"Config file {path} must contain a JSON object, got {type(raw).__name__}"
        )

    server_url = raw.get("server_url")
    if not server_url or not isinstance(server_url, str):
        raise ConfigError(
            f"Config file {path} is missing required field 'server_url' (string)"
        )

    key_file = _expand(raw.get("key_file", DEFAULT_KEY_FILE))
    federation_key = _load_federation_key(key_file)

    ca_file_value = raw.get("ca_file")
    ca_file = _expand(ca_file_value) if ca_file_value else None
    if ca_file is not None and not ca_file.exists():
        raise ConfigError(f"Config field 'ca_file' does not exist: {ca_file}")

    poll_interval = raw.get("poll_interval", DEFAULT_POLL_INTERVAL_SECONDS)
    if (
        not isinstance(poll_interval, int | float)
        or isinstance(poll_interval, bool)
        or poll_interval <= 0
    ):
        raise ConfigError(
            f"Config field 'poll_interval' must be a positive number, got {poll_interval!r}"
        )

    sort = raw.get("sort", DEFAULT_SORT_MODE)
    if sort not in VALID_SORT_MODES:
        raise ConfigError(
            f"Config field 'sort' must be one of {VALID_SORT_MODES}, got {sort!r}"
        )

    focus_app = raw.get("focus_app", "")
    if not isinstance(focus_app, str):
        raise ConfigError(
            f"Config field 'focus_app' must be a string (macOS app name), "
            f"got {focus_app!r}"
        )

    return Config(
        server_url=server_url.rstrip("/"),
        federation_key=federation_key,
        ca_file=ca_file,
        poll_interval=float(poll_interval),
        sort=sort,
        focus_app=focus_app.strip(),
    )


# ---------------------------------------------------------------------------
# Raw config file access -- drives `muxplex-deck config` (list/get/set/reset)
# ---------------------------------------------------------------------------
#
# These operate on the *unvalidated* JSON dict, unlike `load_config()` above
# (which returns a fully-validated `Config`, fails loud on any problem, and
# is what `run()` actually uses). The CLI's config commands intentionally
# tolerate a missing/partial file -- `config list` on a fresh install should
# show defaults, not crash -- mirroring muxplex's own
# load_settings/save_settings/patch_settings pattern (defaults-merge-overlay,
# known-keys-only filtering).


def load_raw_config(config_path: str | None = None) -> dict:
    """Load the config file, merging saved values over `DEFAULT_CONFIG`.

    Returns `DEFAULT_CONFIG` (a copy) if the file does not exist or contains
    corrupt JSON. Unknown keys in the file are ignored.
    """
    import copy

    result = copy.deepcopy(DEFAULT_CONFIG)
    path = _resolve_config_path(config_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for key in DEFAULT_CONFIG:
                if key in data:
                    result[key] = data[key]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return result


def save_raw_config(data: dict, config_path: str | None = None) -> None:
    """Save `data` to the config file, merging with `DEFAULT_CONFIG` first.

    Only known keys are written; creates parent directories as needed.
    """
    import copy

    merged = copy.deepcopy(DEFAULT_CONFIG)
    for key in DEFAULT_CONFIG:
        if key in data:
            merged[key] = data[key]
    path = _resolve_config_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


def patch_raw_config(patch: dict, config_path: str | None = None) -> dict:
    """Merge known keys from `patch` into the current config, save, and return the result."""
    current = load_raw_config(config_path)
    for key in DEFAULT_CONFIG:
        if key in patch:
            current[key] = patch[key]
    save_raw_config(current, config_path)
    return current
