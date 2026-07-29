"""End-to-end hot-reload wiring: `main._run_active`'s poll-loop tick actually

drives `config.ConfigWatcher` -> `_ActiveRuntime.apply_reload` ->
`status.json`, using the same `FakeDeck`/`FakeClient` fakes as
test_runtime_modes.py/test_new_actions.py (no hardware, no server, no real
threads left dangling). `test_config_reload.py` covers the `ConfigWatcher`
contract in isolation; this file proves the wiring `main.py` does with it.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import cast

import pytest
from muxplex_client import MuxplexClient, Settings
from test_runtime_modes import FakeClient, FakeDeck, _make_sessions

from muxplex_deck import main as main_mod
from muxplex_deck.config import ConfigWatcher, load_config
from muxplex_deck.device import DeckDevice
from muxplex_deck.main import _ActiveRuntime
from muxplex_deck.statusfile import StatusReporter, read_status

SETTINGS = Settings(views=(), hidden_sessions=frozenset(), sort_order="manual")


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _bump_mtime(path: Path) -> None:
    current = path.stat().st_mtime
    os.utime(path, (current + 5, current + 5))


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    key_file = tmp_path / "federation_key"
    key_file.write_text("sekrit\n", encoding="utf-8")
    path = tmp_path / "config.json"
    _write_config(
        path,
        {
            "server_url": "https://example.test:8088",
            "key_file": str(key_file),
            "controls": {},
        },
    )
    return path


def _make_reduced_deck() -> FakeDeck:
    return FakeDeck(
        key_count=15, key_layout=(3, 5), key_size=(72, 72), dial_count=0, is_touch=False
    )


class TestHotReloadWiring:
    def test_controls_edit_mid_session_is_applied_and_published(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact user scenario: edit `controls` while the sidecar is up,

        no restart -- `_run_active`'s loop picks it up on its own poll tick
        and publishes the outcome to status.json.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        reporter = StatusReporter("https://example.test:8088", tmp_path / "status.json")
        shutting_down = threading.Event()

        ticks = {"n": 0}

        def _fake_wait(
            wait_deck: DeckDevice, event: threading.Event, seconds: float
        ) -> bool:
            ticks["n"] += 1
            if ticks["n"] == 1:
                # Simulate the user running `controls set key.11 view_next`
                # while the sidecar is already up and polling.
                data = json.loads(config_path.read_text(encoding="utf-8"))
                data["controls"] = {"key.11": "view_next"}
                _write_config(config_path, data)
                _bump_mtime(config_path)
            if ticks["n"] >= 3:
                event.set()
            return event.is_set()

        monkeypatch.setattr(main_mod, "_interruptible_wait", _fake_wait)

        main_mod._run_active(
            cast(DeckDevice, deck),
            cast(MuxplexClient, client),
            shutting_down,
            "test-server",
            reporter,
            watcher,
        )

        assert watcher.current.controls == {"key.11": "view_next"}

        status = read_status(tmp_path / "status.json")
        assert status is not None
        reload_status = status["config_reload"]
        assert reload_status["applied"] == ["controls"]
        assert reload_status["error"] is None
        assert reload_status["restart_required"] == []

    def test_restart_required_field_is_reported_never_applied_mid_session(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        reporter = StatusReporter("https://example.test:8088", tmp_path / "status.json")
        shutting_down = threading.Event()
        ticks = {"n": 0}

        def _fake_wait(
            wait_deck: DeckDevice, event: threading.Event, seconds: float
        ) -> bool:
            ticks["n"] += 1
            if ticks["n"] == 1:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                data["server_url"] = "https://different.test:8088"
                _write_config(config_path, data)
                _bump_mtime(config_path)
            if ticks["n"] >= 3:
                event.set()
            return event.is_set()

        monkeypatch.setattr(main_mod, "_interruptible_wait", _fake_wait)

        main_mod._run_active(
            cast(DeckDevice, deck),
            cast(MuxplexClient, client),
            shutting_down,
            "test-server",
            reporter,
            watcher,
        )

        status = read_status(tmp_path / "status.json")
        assert status is not None
        reload_status = status["config_reload"]
        assert reload_status["applied"] == []
        assert reload_status["restart_required"] == ["server_url"]

    def test_bad_edit_mid_session_keeps_last_good_bindings_no_crash(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-edit that fails Gate 1 while the sidecar is already up must

        not crash the active session or blank the deck's real bindings --
        it's reported via `status.json`'s `config_reload.error` and the
        session keeps running under whatever it had before.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        reporter = StatusReporter("https://example.test:8088", tmp_path / "status.json")
        shutting_down = threading.Event()
        ticks = {"n": 0}

        def _fake_wait(
            wait_deck: DeckDevice, event: threading.Event, seconds: float
        ) -> bool:
            ticks["n"] += 1
            if ticks["n"] == 1:
                config_path.write_text("{not valid json", encoding="utf-8")
                _bump_mtime(config_path)
            if ticks["n"] >= 3:
                event.set()
            return event.is_set()

        monkeypatch.setattr(main_mod, "_interruptible_wait", _fake_wait)

        # Must not raise.
        main_mod._run_active(
            cast(DeckDevice, deck),
            cast(MuxplexClient, client),
            shutting_down,
            "test-server",
            reporter,
            watcher,
        )

        assert watcher.current.controls == {}  # unchanged -- last-known-good

        status = read_status(tmp_path / "status.json")
        assert status is not None
        reload_status = status["config_reload"]
        assert reload_status["error"] is not None
        assert reload_status["applied"] == []

    def test_no_edit_never_publishes_config_reload_field(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No touch to config.json -> `config_reload` stays absent (never

        checked). Distinguishes "nothing changed" from "checked and found
        no reloadable delta" -- the latter still has a `config_reload`
        record, just with empty `applied`.
        """
        initial = load_config(str(config_path))
        watcher = ConfigWatcher(str(config_path), initial)

        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        reporter = StatusReporter("https://example.test:8088", tmp_path / "status.json")
        shutting_down = threading.Event()
        ticks = {"n": 0}

        def _fake_wait(
            wait_deck: DeckDevice, event: threading.Event, seconds: float
        ) -> bool:
            ticks["n"] += 1
            if ticks["n"] >= 2:
                event.set()
            return event.is_set()

        monkeypatch.setattr(main_mod, "_interruptible_wait", _fake_wait)

        main_mod._run_active(
            cast(DeckDevice, deck),
            cast(MuxplexClient, client),
            shutting_down,
            "test-server",
            reporter,
            watcher,
        )

        status = read_status(tmp_path / "status.json")
        assert status is not None
        assert status.get("config_reload") is None


class TestApplyReload:
    """Direct unit tests of `_ActiveRuntime.apply_reload` -- the piece

    `_run_active`'s loop calls when `ConfigWatcher.poll()` reports an
    applied change.
    """

    def _make_ctx(self, deck: FakeDeck, client: FakeClient) -> _ActiveRuntime:
        ctx = _ActiveRuntime(
            deck=cast(DeckDevice, deck),
            client=cast(MuxplexClient, client),
            hostname="test-server",
            sort_mode="server",
            controls={},
            poll_interval=2.0,
        )
        ctx.refresh()
        return ctx

    def test_new_control_binding_changes_the_resolved_plan(self) -> None:
        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = self._make_ctx(deck, client)
        assert ctx.plan.bindings["key.11"] == "session"

        from muxplex_deck.config import Config

        reloaded = Config(
            server_url="https://example.test:8088",
            federation_key="sekrit",
            ca_file=None,
            poll_interval=2.0,
            sort="server",
            controls={"key.11": "view_next"},
            focus_app="",
        )

        ctx.apply_reload(reloaded)

        assert ctx.plan.bindings["key.11"] == "view_next"
        # 12 session slots by default (15 keys - 3 reserved); key.11 moving
        # to view_next drops it to 11.
        assert ctx.plan.sessions_per_page == 11

    def test_sort_focus_app_and_poll_interval_are_updated(self) -> None:
        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = self._make_ctx(deck, client)

        from muxplex_deck.config import Config

        reloaded = Config(
            server_url="https://example.test:8088",
            federation_key="sekrit",
            ca_file=None,
            poll_interval=7.5,
            sort="attention",
            controls={},
            focus_app="muxplex",
        )

        ctx.apply_reload(reloaded)

        assert ctx.sort_mode == "attention"
        assert ctx.focus_app_name == "muxplex"
        assert ctx.poll_interval == 7.5

    def test_paint_cache_is_invalidated_so_next_repaint_redraws_everything(
        self,
    ) -> None:
        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = self._make_ctx(deck, client)
        ctx.repaint()
        assert any(state is not None for state in ctx.last_key_state)

        from muxplex_deck.config import Config

        reloaded = Config(
            server_url="https://example.test:8088",
            federation_key="sekrit",
            ca_file=None,
            poll_interval=2.0,
            sort="server",
            controls={},
            focus_app="",
        )
        ctx.apply_reload(reloaded)

        assert all(state is None for state in ctx.last_key_state)
        assert ctx.last_strip is None
