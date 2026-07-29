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

from . import controls as controls_mod

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
    # address -> action; empty means "all capability-derived defaults" (see
    # `layout.default_bindings`). Defaults are computed, never stored here --
    # a fresh install has no "controls" key at all and sees zero behavior
    # change. See docs/CONTROL_MAPPING_DESIGN.md.
    "controls": {},
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
    if text.startswith(("~/", "~\\")):
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
    controls: dict[str, str]
    """Resolved (address -> action) overrides, Gate-1 validated (grammar +

    catalog + kind), but NOT yet checked against any deck's capabilities --
    that is Gate 2 (`layout.plan_layout`), which runs later because at
    config-load time there may be no deck plugged in at all. Empty dict
    means "use the capability-derived defaults for whatever deck connects".
    See docs/CONTROL_MAPPING_DESIGN.md.
    """
    focus_app: str
    """Identifier for the locally installed muxplex PWA, used to bring it to
    the foreground when a key press switches the active session (see
    `.focus`). Empty (the default) disables the feature entirely -- no
    subprocess/API calls, no log noise.

    Meaning is platform-specific -- one field, two interpretations, not a
    second config key, because each platform has exactly one natural way
    to address "the PWA" and they're different shapes: macOS's PWA is a
    real, individually-launchable `.app` bundle (addressed by name, via
    `open -a`); Windows' runs inside a generic browser process with no
    per-app identity of its own, so the only thing that reliably names it
    is its window TITLE (addressed by substring match). Concretely:

    - macOS: the `.app` bundle name (as `open -a <name>` expects).
    - Windows: a substring to match against a top-level window's title
      (see `focus._focus_windows` for the matching + foreground-steal
      mechanics, and its real, documented limits).
    - Linux/WSL: no implementation exists yet; a configured value logs one
      INFO notice per process and is otherwise ignored.

    Existing macOS configs are unaffected -- this only changes what the
    same field means when read on Windows.
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


def _validate_controls(raw_controls: object) -> dict[str, str]:
    """Gate 1 (§6): capability-blind validation of the `controls` config field.

    Fails closed -- raises `ConfigError` for anything decidable from the
    file alone: not an object, a non-string value, a malformed address, an
    unknown action, or a kind mismatch (e.g. a momentary action bound to a
    dial turn). What this does NOT check -- whether an address's index is
    within a real deck's actual key/dial count -- is Gate 2
    (`layout.plan_layout`), which runs later because there may be no deck
    plugged in at config-load time at all.
    """
    if not isinstance(raw_controls, dict):
        raise ConfigError(
            "Config field 'controls' must be a JSON object, got "
            f"{type(raw_controls).__name__}"
        )
    validated: dict[str, str] = {}
    for address_text, action in raw_controls.items():
        if not isinstance(action, str):
            raise ConfigError(
                f"Config field 'controls' value for {address_text!r} must be "
                f"a string, got {action!r}"
            )
        try:
            address = controls_mod.parse_address(address_text)
        except controls_mod.AddressError as exc:
            raise ConfigError(f"Config field 'controls' {exc}") from exc
        if action not in controls_mod.ACTIONS:
            raise ConfigError(
                f"Config field 'controls' has unknown action {action!r} for "
                f"{address_text!r}. Valid actions: "
                f"{', '.join(sorted(controls_mod.ACTIONS))}"
            )
        valid_for_address = controls_mod.valid_actions_for_address(address)
        if action not in valid_for_address:
            target = "a dial turn" if address.is_relative_only else "a key/dial push"
            raise ConfigError(
                f"Config field 'controls': action {action!r} cannot be bound "
                f"to {address_text!r} -- {target} accepts only: "
                f"{', '.join(valid_for_address)}"
            )
        validated[address.text] = action
    return validated


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
            "Run: muxplex-deck init\n"
            "(or create it by hand with at least a 'server_url' field, e.g.:\n"
            '  {"server_url": "https://<your-server>:8088"})'
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
            "Config field 'focus_app' must be a string (macOS: .app bundle "
            "name; Windows: browser window title substring), "
            f"got {focus_app!r}"
        )

    controls_value = _validate_controls(raw.get("controls", {}))

    return Config(
        server_url=server_url.rstrip("/"),
        federation_key=federation_key,
        ca_file=ca_file,
        poll_interval=float(poll_interval),
        sort=sort,
        controls=controls_value,
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


# ---------------------------------------------------------------------------
# Hot reload -- pick up `controls set`/`config set` edits without a restart.
# ---------------------------------------------------------------------------
#
# The running sidecar and the CLI process that edits config.json are always
# different processes. Rather than a new IPC channel (socket, HTTP endpoint),
# this follows the repo's existing file-based-IPC precedent (`statusfile.py`,
# `singleton.py`): the sidecar's already-running poll loop cheaply `stat()`s
# config.json on its existing tick (`main._run_active`'s while loop, same
# cadence as the server poll) and, only when the mtime actually changed,
# re-runs it through the exact same Gate 1 validation `load_config` performs
# at startup (`ConfigError` -> keep the last-known-good `Config`, never a
# partially-applied one -- see `ConfigWatcher.poll`'s docstring).
#
# Not every key is safe to apply live. `server_url`, `key_file` (whose
# resolved value is `Config.federation_key`), and `ca_file` are baked into a
# `MuxplexClient` constructed once per connection (`main._run`); swapping
# them out from under a live `httpx.Client`/TLS context is not attempted --
# changing any of them is reported (`ReloadOutcome.restart_required`) but
# never applied, and a sidecar restart is still required. Everything else in
# `DEFAULT_CONFIG` is read fresh on every tick already (`controls`/`sort`/
# `focus_app` are plain values threaded through `_ActiveRuntime`;
# `poll_interval` is a local variable in `_run_active`'s wait call) --
# verified by inspection, not assumed: none of them are captured into a
# closure, a constructed client, or any other object that would go stale.
RELOADABLE_KEYS: tuple[str, ...] = ("controls", "sort", "focus_app", "poll_interval")

# (report name as it appears in config.json / `config list`, Config attribute
# to compare) -- reported when different, but NEVER applied to the running
# session; a restart is required. `key_file` compares on `federation_key`
# (the resolved secret), not the raw path, since a path change is only
# meaningful insofar as it changes which key is live.
_RESTART_REQUIRED_FIELDS: tuple[tuple[str, str], ...] = (
    ("server_url", "server_url"),
    ("key_file", "federation_key"),
    ("ca_file", "ca_file"),
)


@dataclass(frozen=True)
class ReloadOutcome:
    """Result of one `ConfigWatcher.poll()` check.

    `checked` is False when config.json's mtime hasn't changed since the
    last check -- the common case, and deliberately cheap: just one
    `Path.stat()` call, no re-read, no re-validation (see module docstring
    on poll cost). Callers should treat `checked=False` as "nothing to do
    at all", not merely "nothing changed".

    When `checked` is True:
    - `error` is set (and `config` is `None`) if the file changed but
      failed Gate 1 validation -- the caller's previously-held `Config`
      must be kept exactly as-is, as if this poll had not run. This is
      the fail-safe behavior for a bad hand-edit at runtime: unlike
      startup (which fails closed with a non-zero exit), a sidecar
      already driving hardware keeps its last-good bindings and only
      reports the problem.
    - Otherwise `config` is the freshly loaded, fully Gate-1-validated
      `Config` (whether or not anything reloadable actually differs).
      `applied` lists which of `RELOADABLE_KEYS` differ from the
      previous known-good config -- empty if the file changed but only a
      restart-required (or no) field did. `restart_required` lists
      non-reloadable keys that differ -- informational only, never
      applied; the field the running session is still using is
      unaffected.
    """

    checked: bool
    config: Config | None = None
    applied: tuple[str, ...] = ()
    restart_required: tuple[str, ...] = ()
    error: str | None = None


class ConfigWatcher:
    """Detects and Gate-1-validates config.json changes for a running sidecar.

    Constructed once per `main._run()` invocation with whatever `Config` was
    loaded at startup; `poll()` is called from the existing poll-loop tick
    in `main._run_active`. See `RELOADABLE_KEYS`/`ReloadOutcome` for the
    contract. `current` always holds the last-known-good `Config` -- never
    a config that failed validation, and never one with a bad-edit's
    unsafe fields silently substituted in.
    """

    def __init__(self, config_path: str | None, initial: Config) -> None:
        self._config_path = config_path
        self._resolved_path = _resolve_config_path(config_path)
        self._last_good = initial
        self._mtime = self._stat_mtime()

    def _stat_mtime(self) -> float | None:
        try:
            return self._resolved_path.stat().st_mtime
        except OSError:
            return None

    @property
    def current(self) -> Config:
        """The last-known-good `Config` -- what the sidecar should be using right now."""
        return self._last_good

    def poll(self) -> ReloadOutcome:
        """Cheap check: has config.json changed since the last check?

        Only re-reads/re-validates the file when its mtime differs from
        what was last observed -- the common per-tick case is a single
        `stat()` call and nothing else.
        """
        mtime = self._stat_mtime()
        if mtime == self._mtime:
            return ReloadOutcome(checked=False)
        # Record the new mtime regardless of outcome: a broken edit must be
        # reported once, not re-validated (and re-logged) on every future
        # tick until it's fixed -- the file genuinely changed, so there is
        # nothing more to learn from re-parsing the same bytes again.
        self._mtime = mtime

        try:
            new_config = load_config(self._config_path)
        except ConfigError as exc:
            return ReloadOutcome(checked=True, error=str(exc))

        applied = tuple(
            key
            for key in RELOADABLE_KEYS
            if getattr(new_config, key) != getattr(self._last_good, key)
        )
        restart_required = tuple(
            report_name
            for report_name, attr in _RESTART_REQUIRED_FIELDS
            if getattr(new_config, attr) != getattr(self._last_good, attr)
        )
        self._last_good = new_config
        return ReloadOutcome(
            checked=True,
            config=new_config,
            applied=applied,
            restart_required=restart_required,
        )
