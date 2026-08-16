"""Device identity for the muxplex-deck sidecar.

Deck-side "Step 1" (walking skeleton) of
``muxplex/docs/plans/2026-08-16-deck-control-target-design.md`` §8.3/§8.4/§10:
this deck needs a stable ``device_id`` to pass on every group-touching
``muxplex_client`` call (``state()``/``connect()``/``set_active_view()``) and
on ``heartbeat()``, so the server's device registry can recognize it across
restarts instead of minting a fresh, unrelated identity every launch --
exactly the churn the design doc flags as a real risk for a future picker UI
(assumption 2: "Physical decks are 1-per-host and long-lived; a persisted
deck identity is acceptable").

Mirrors muxplex's own ``muxplex/identity.py`` convention exactly (a
persistent UUID v4, generated once and cached in a small JSON file) --
same shape, same generate-on-miss behavior -- but colocated with THIS
sidecar's own config directory rather than a hardcoded ``~/.config/muxplex``
path: ``identity.json`` lives next to ``config.json``, resolved through
``config._resolve_config_path`` so ``--config``/``MUXPLEX_DECK_CONFIG``
override it exactly like they override the config file itself, and so the
test suite's existing Rail 1 (which redirects ``MUXPLEX_DECK_CONFIG`` to a
per-test tmp file) isolates this file automatically with no separate rail
needed.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from . import config as config_mod

_IDENTITY_FILE_NAME = "identity.json"


def default_identity_path(config_path: str | None = None) -> Path:
    """``identity.json``, next to whichever ``config.json`` is in effect.

    Reuses `config_mod._resolve_config_path` (the same ``--config`` /
    ``MUXPLEX_DECK_CONFIG`` / default-path resolution `load_config` itself
    uses) so this file always lives beside the config it's paired with --
    including under test, where Rail 1 already redirects that resolution
    to a per-test tmp file.
    """
    return config_mod._resolve_config_path(config_path).parent / _IDENTITY_FILE_NAME


def load_device_id(config_path: str | None = None) -> str:
    """Load this sidecar's persistent ``device_id``, minting one if needed.

    If `default_identity_path`'s file is absent, corrupt, or missing the
    ``device_id`` key, a new UUID v4 is generated, written to the file
    (creating parent directories as needed), and returned -- mirroring
    `muxplex.identity.load_device_id`'s exact contract. Called once at
    sidecar bring-up (`main._run`); the returned id is then held for the
    lifetime of the process and reused across reconnects, so a deck's
    identity survives a hotplug/replug or a server-unreachable backoff
    without churning.
    """
    path = default_identity_path(config_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        device_id = data["device_id"]
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id is not a non-empty string")
        return device_id
    except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError):
        return _generate_and_save(path)


def _generate_and_save(path: Path) -> str:
    """Generate a new UUID v4, persist it to `path`, and return it."""
    device_id = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"device_id": device_id}, indent=2) + "\n", encoding="utf-8"
    )
    return device_id
