"""`config.ConfigWatcher` / `config.ReloadOutcome` -- hot-reload detection for

a running sidecar's config.json (docs: config.py's "Hot reload" section).

No sidecar, no device, no server involved here -- purely the file-watching
and Gate-1-revalidation contract. `main.py`'s wiring of this into the poll
loop (`_run_active`/`_ActiveRuntime.apply_reload`) is covered separately in
`test_main_logging.py`/`test_runtime_modes.py`-style fakes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from muxplex_deck import config as config_mod
from muxplex_deck.config import ConfigWatcher, load_config


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    """Force a detectably different mtime, independent of filesystem/clock

    resolution -- some filesystems (and fast test runs) can produce two
    writes with an identical `st_mtime` otherwise.
    """
    current = path.stat().st_mtime
    os.utime(path, (current + 5, current + 5))


BASE_CONFIG: dict = {
    "server_url": "https://example.test:8088",
    "key_file": "",  # overridden per-test to point at a real key file
    "ca_file": "",
    "poll_interval": 2.0,
    "sort": "attention",
    "controls": {},
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    key_file = tmp_path / "federation_key"
    key_file.write_text("sekrit\n", encoding="utf-8")
    path = tmp_path / "config.json"
    data = dict(BASE_CONFIG)
    data["key_file"] = str(key_file)
    _write_config(path, data)
    return path


class TestNoReloadWhenUnchanged:
    def test_poll_without_touching_file_never_reloads(self, config_path: Path) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        outcome = watcher.poll()

        assert outcome.checked is False
        assert outcome.config is None
        assert outcome.applied == ()
        assert outcome.error is None
        assert watcher.current is initial

    def test_repeated_polls_stay_unchecked(self, config_path: Path) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)
        for _ in range(5):
            assert watcher.poll().checked is False


class TestReloadApplied:
    def test_controls_change_is_detected_and_applied(self, config_path: Path) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["controls"] = {"key.0": "view_picker"}
        _write_config(config_path, data)
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.checked is True
        assert outcome.error is None
        assert outcome.config is not None
        assert outcome.applied == ("controls",)
        assert outcome.restart_required == ()
        assert watcher.current.controls == {"key.0": "view_picker"}

    def test_sort_is_reloadable(self, config_path: Path) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["sort"] = "server"
        _write_config(config_path, data)
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert set(outcome.applied) == {"sort"}
        assert watcher.current.sort == "server"

    def test_poll_interval_is_reloadable(self, config_path: Path) -> None:
        """Verified, not assumed: `poll_interval` is a local variable in

        `_run_active`'s wait call, never captured into a client or a
        closure -- see config.py's `RELOADABLE_KEYS` docstring.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["poll_interval"] = 5.0
        _write_config(config_path, data)
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.applied == ("poll_interval",)
        assert watcher.current.poll_interval == 5.0

    def test_no_op_write_reports_no_applied_keys(self, config_path: Path) -> None:
        """The file's mtime changed but no reloadable value actually did --

        `checked` is True (a real re-validation ran) but `applied` is empty.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        _write_config(config_path, data)  # byte-identical content
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.checked is True
        assert outcome.error is None
        assert outcome.applied == ()
        assert outcome.restart_required == ()


class TestRestartRequiredFields:
    def test_server_url_change_is_reported_but_not_applied(
        self, config_path: Path
    ) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["server_url"] = "https://different.test:8088"
        _write_config(config_path, data)
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.applied == ()  # server_url is not a RELOADABLE_KEYS member
        assert outcome.restart_required == ("server_url",)

    def test_ca_file_and_key_file_changes_are_restart_required(
        self, config_path: Path, tmp_path: Path
    ) -> None:
        other_key = tmp_path / "other_key"
        other_key.write_text("different-secret\n", encoding="utf-8")
        other_ca = tmp_path / "other_ca.pem"
        other_ca.write_text("not a real cert, just needs to exist", encoding="utf-8")

        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["key_file"] = str(other_key)
        data["ca_file"] = str(other_ca)
        _write_config(config_path, data)
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert set(outcome.restart_required) == {"key_file", "ca_file"}
        assert outcome.applied == ()
        # `.current` DOES pick up the new values (it's "last known-good",
        # not "what the live client is using") -- but nothing in the
        # running session re-reads it for the connection itself; see
        # `main._run`'s outer loop, which keeps using its own original
        # `config` for `MuxplexClient(...)`, never `watcher.current`.
        assert watcher.current.federation_key == "different-secret"


class TestBadEditKeepsLastGood:
    def test_invalid_json_keeps_previous_config(self, config_path: Path) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        config_path.write_text("{not valid json", encoding="utf-8")
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.checked is True
        assert outcome.config is None
        assert outcome.error is not None
        assert "not valid JSON" in outcome.error
        assert watcher.current is initial  # unchanged, byte-for-byte the old object

    def test_gate1_rejection_keeps_previous_config(self, config_path: Path) -> None:
        """A hand-edit that fails Gate 1 (`_validate_controls`) at runtime --

        e.g. an unknown action -- must never crash the sidecar or silently
        adopt a partially-valid config; the last-known-good bindings stay
        in effect and the problem is reported via `ReloadOutcome.error`.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        data = json.loads(config_path.read_text(encoding="utf-8"))
        data["controls"] = {"key.0": "not-a-real-action"}
        _write_config(config_path, data)
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.error is not None
        assert "unknown action" in outcome.error
        assert watcher.current.controls == {}  # last-good, not the bad edit

    def test_missing_required_field_keeps_previous_config(
        self, config_path: Path
    ) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        config_path.write_text(
            json.dumps({"controls": {}}), encoding="utf-8"
        )  # no server_url at all
        _bump_mtime(config_path)

        outcome = watcher.poll()

        assert outcome.error is not None
        assert watcher.current.server_url == initial.server_url

    def test_recovers_on_next_valid_edit_after_a_bad_one(
        self, config_path: Path
    ) -> None:
        """A fixed-up follow-on edit is picked up normally -- one bad poll

        doesn't wedge the watcher permanently.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        config_path.write_text("{not valid json", encoding="utf-8")
        _bump_mtime(config_path)
        bad_outcome = watcher.poll()
        assert bad_outcome.error is not None

        fixed = dict(BASE_CONFIG)
        fixed["key_file"] = str(config_path.parent / "federation_key")
        fixed["sort"] = "server"
        _write_config(config_path, fixed)
        _bump_mtime(config_path)

        good_outcome = watcher.poll()

        assert good_outcome.error is None
        assert good_outcome.config is not None
        assert "sort" in good_outcome.applied
        assert watcher.current.sort == "server"


class TestReloadableKeysAccounting:
    def test_every_default_config_key_is_reloadable_or_restart_required(self) -> None:
        """Full accounting: no `DEFAULT_CONFIG` key is silently unaccounted for.

        `poll_interval` and `key_file` compare on different attribute names
        (`key_file` -> `federation_key`), which is why this checks by
        report-name set rather than a straight attribute walk.
        """
        reloadable = set(config_mod.RELOADABLE_KEYS)
        restart_required = {name for name, _attr in config_mod._RESTART_REQUIRED_FIELDS}
        accounted = reloadable | restart_required
        assert accounted == set(config_mod.DEFAULT_CONFIG)


class TestMtimeTracking:
    def test_construction_snapshots_current_mtime_no_immediate_reload(
        self, config_path: Path
    ) -> None:
        """A watcher constructed against an already-on-disk file must not

        treat that file's existing mtime as a "change" on the very first
        poll.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)
        assert watcher.poll().checked is False

    def test_missing_file_at_construction_then_created_is_detected(
        self, tmp_path: Path
    ) -> None:
        missing_path = tmp_path / "does-not-exist-yet.json"
        key_file = tmp_path / "federation_key"
        key_file.write_text("sekrit\n", encoding="utf-8")

        # Seed an initial in-memory Config as if it were loaded from
        # elsewhere (watcher never requires the file to exist at construction).
        initial = load_config(str(_seed_config(tmp_path, key_file)))
        watcher = ConfigWatcher(str(missing_path), initial)
        assert watcher.poll().checked is False  # still absent -- nothing to detect

        data = dict(BASE_CONFIG)
        data["key_file"] = str(key_file)
        _write_config(missing_path, data)

        outcome = watcher.poll()
        assert outcome.checked is True
        assert outcome.error is None


def _seed_config(tmp_path: Path, key_file: Path) -> Path:
    path = tmp_path / "seed-config.json"
    data = dict(BASE_CONFIG)
    data["key_file"] = str(key_file)
    _write_config(path, data)
    return path
