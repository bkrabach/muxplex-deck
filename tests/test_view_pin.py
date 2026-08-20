"""`Config.view_pin` / `_ActiveRuntime` deck-local view pinning ("Step 0").

Extends `muxplex/docs/plans/2026-08-16-deck-control-target-design.md`'s
Alternative A: an optional, config-driven local "view pin" that stops this
deck's dial-0 view changes from `PATCH`ing the server's (global)
`active_view` -- the sidecar's single biggest live annoyance, since a
server-global PATCH yanks every OTHER connected client's view on every
turn. Unset (the default), behavior is byte-identical to today -- see
`TestNoPinIsByteIdenticalRegression` below, the load-bearing test in this
file.

Uses the same `FakeDeck`/`FakeClient` fakes as test_runtime_modes.py/
test_new_actions.py (no hardware, no server, no real threads left
dangling) and the same hot-reload wiring pattern as test_hot_reload.py/
test_config_reload.py.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import cast

import pytest
from muxplex_client import MuxplexClient, Settings, View
from test_runtime_modes import FakeClient, FakeDeck, _make_sessions

from muxplex_deck import main as main_mod
from muxplex_deck.config import Config, ConfigWatcher, load_config
from muxplex_deck.device import DeckDevice
from muxplex_deck.interaction import ViewCycler
from muxplex_deck.main import _ActiveRuntime
from muxplex_deck.statusfile import StatusReporter, read_status

_TEST_DEBOUNCE_SECONDS = 0.05
_WAIT_SECONDS = 5.0

SETTINGS = Settings(
    views=(View(name="focus", sessions=frozenset({"session-00", "session-01"})),),
    hidden_sessions=frozenset(),
    sort_order="manual",
)


def _wait_until(predicate, timeout: float = _WAIT_SECONDS, poll: float = 0.01) -> bool:
    """Poll `predicate` until it's True or `timeout` elapses.

    `_commit_view`'s pinned branch never touches `client.view_patch_event`
    (there is no PATCH to flag), so tests that need to wait for a
    debounced dial-turn commit to land can't wait on that event the way
    the pre-existing unpinned tests do -- this polls the runtime's own
    state instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return predicate()


def _make_full_deck() -> FakeDeck:
    return FakeDeck(
        key_count=8, key_layout=(2, 4), key_size=(120, 120), dial_count=4, is_touch=True
    )


def _make_runtime(
    deck: FakeDeck, client: FakeClient, *, view_pin: str | None = None
) -> _ActiveRuntime:
    ctx = _ActiveRuntime(
        deck=cast(DeckDevice, deck),
        client=cast(MuxplexClient, client),
        hostname="test-server",
        sort_mode="server",
        view_pin=view_pin,
    )
    ctx.view_cycler = ViewCycler(debounce_seconds=_TEST_DEBOUNCE_SECONDS)
    return ctx


class TestPinnedDialTurnNeverPatches:
    """Turning dial 0 while pinned must never call `set_active_view`."""

    def test_dial_turn_while_pinned_does_not_patch_server(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, view_pin="all")
        ctx.refresh()

        ctx.handle_dial_turn(0, "view_cycle", 1)

        assert _wait_until(lambda: ctx.active_view == "focus")
        assert client.view_patches == []

    def test_dial_turn_while_pinned_updates_local_state_and_filtered_list(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, view_pin="all")
        ctx.refresh()

        ctx.handle_dial_turn(0, "view_cycle", 1)  # "all" -> "focus"

        assert _wait_until(lambda: ctx.active_view == "focus")
        assert ctx.view_pin == "focus"
        # The rendered/filtered session list reflects the new pin: only
        # the two sessions "focus" contains, not all 20.
        assert _wait_until(
            lambda: (
                {s.name for s in ctx.ordered}
                == {
                    "session-00",
                    "session-01",
                }
            )
        )


class TestProcessIgnoresServerActiveViewWhenPinned:
    def test_process_keeps_pinned_view_despite_divergent_server_state(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, view_pin="focus")
        ctx.refresh()
        assert ctx.active_view == "focus"
        assert {s.name for s in ctx.ordered} == {"session-00", "session-01"}

        # The server (e.g. the PWA, or another device) switches its own
        # global active_view to "all" -- a pinned deck must not follow.
        client.active_view = "all"
        ctx.refresh()

        assert ctx.active_view == "focus"
        assert {s.name for s in ctx.ordered} == {"session-00", "session-01"}

    def test_process_never_resets_pager_off_server_side_view_changes_when_pinned(
        self,
    ) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, view_pin="all")
        ctx.refresh()
        ctx.handle_dial_turn(1, "page_cycle", 1)
        assert ctx.pager.page == 2

        # A server-side view change (unrelated to this deck's pin) must
        # not reset paging, since the pinned deck never adopted it.
        client.active_view = "focus"
        ctx.refresh()

        assert ctx.active_view == "all"
        assert ctx.pager.page == 2


class TestNoPinIsByteIdenticalRegression:
    """The load-bearing test: with `view_pin` unset, nothing changes.

    Mirrors test_runtime_modes.py's own
    `test_view_dial_turn_commits_debounced_patch`/`test_page_resets_on_view_change`
    exactly, but constructed through this file's own `_make_runtime` (which
    now threads `view_pin` through `_ActiveRuntime.__init__`) to prove the
    new parameter's default doesn't change anything when omitted.
    """

    def test_dial_turn_still_patches_server_when_unpinned(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)  # view_pin defaults to None
        ctx.refresh()
        assert ctx.view_pin is None

        ctx.handle_dial_turn(0, "view_cycle", 1)

        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["focus"]

    def test_process_still_adopts_server_active_view_when_unpinned(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.active_view == "all"

        client.active_view = "focus"  # server-side view change (e.g. the PWA)
        ctx.refresh()

        assert ctx.active_view == "focus"
        assert {s.name for s in ctx.ordered} == {"session-00", "session-01"}

    def test_page_resets_on_server_side_view_change_when_unpinned(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        ctx.handle_dial_turn(1, "page_cycle", 1)
        assert ctx.pager.page == 2

        client.active_view = "focus"
        ctx.refresh()

        assert ctx.pager.page == 1  # reset, exactly like before view_pin existed


class TestApplyReloadViewPin:
    """Direct unit tests of `_ActiveRuntime.apply_reload`'s `view_pin` handling."""

    def _reloaded_config(self, *, view_pin: str | None) -> Config:
        return Config(
            server_url="https://example.test:8088",
            federation_key="sekrit",
            ca_file=None,
            poll_interval=2.0,
            sort="server",
            view_pin=view_pin,
            name="",
            controls={},
            font_scale=1.0,
        )

    def test_newly_setting_pin_snaps_active_view_immediately(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.active_view == "all"

        ctx.apply_reload(self._reloaded_config(view_pin="focus"))

        assert ctx.view_pin == "focus"
        assert ctx.active_view == "focus"

    def test_changing_pin_to_a_different_value_snaps_active_view_again(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, view_pin="focus")
        ctx.refresh()

        ctx.apply_reload(self._reloaded_config(view_pin="hidden"))

        assert ctx.view_pin == "hidden"
        assert ctx.active_view == "hidden"

    def test_clearing_pin_needs_no_special_handling_next_process_resumes_tracking(
        self,
    ) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, view_pin="focus")
        ctx.refresh()
        assert ctx.active_view == "focus"

        client.active_view = "all"  # server's own (unfollowed while pinned)
        ctx.apply_reload(self._reloaded_config(view_pin=None))
        assert ctx.view_pin is None
        # apply_reload itself does nothing special for a clear -- active_view
        # is still "focus" until the next _process() call resumes tracking
        # server_state.active_view (see _process's docstring).
        assert ctx.active_view == "focus"

        ctx.refresh()  # the next poll

        assert ctx.active_view == "all"


class TestActiveRuntimeSeedsFromPinAtConstruction:
    def test_constructed_with_a_pin_seeds_active_view_from_it(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        client.active_view = "all"  # server disagrees with the pin from the start
        ctx = _make_runtime(deck, client, view_pin="focus")

        assert ctx.active_view == "focus"

        ctx.refresh()  # first poll -- must keep filtering by the pin, not "all"

        assert ctx.active_view == "focus"
        assert {s.name for s in ctx.ordered} == {"session-00", "session-01"}

    def test_constructed_without_a_pin_seeds_active_view_all_as_before(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)

        assert ctx.active_view == "all"


# ---------------------------------------------------------------------------
# Hot-reload wiring end-to-end -- mirrors test_hot_reload.py's
# TestHotReloadWiring exactly, but for `view_pin`.
# ---------------------------------------------------------------------------


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


class TestViewPinHotReloadWiring:
    def test_setting_view_pin_mid_session_is_applied_without_restart(
        self, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        initial = load_config(str(config_path))
        assert initial.view_pin is None
        watcher = ConfigWatcher(str(config_path), initial)

        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        reporter = StatusReporter("https://example.test:8088", tmp_path / "status.json")
        shutting_down = threading.Event()
        ticks = {"n": 0}

        def _fake_wait(
            wait_deck: DeckDevice, event: threading.Event, seconds: float
        ) -> bool:
            ticks["n"] += 1
            if ticks["n"] == 1:
                # Simulate `muxplex-deck config set view_pin focus` (or a
                # hand-edit) while the sidecar is already up and polling.
                data = json.loads(config_path.read_text(encoding="utf-8"))
                data["view_pin"] = "focus"
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

        assert watcher.current.view_pin == "focus"

        status = read_status(tmp_path / "status.json")
        assert status is not None
        reload_status = status["config_reload"]
        assert reload_status["applied"] == ["view_pin"]
        assert reload_status["error"] is None
        assert reload_status["restart_required"] == []

    def test_clearing_view_pin_mid_session_is_applied_without_restart(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "federation_key"
        key_file.write_text("sekrit\n", encoding="utf-8")
        path = tmp_path / "config.json"
        _write_config(
            path,
            {
                "server_url": "https://example.test:8088",
                "key_file": str(key_file),
                "view_pin": "focus",
                "controls": {},
            },
        )
        initial = load_config(str(path))
        assert initial.view_pin == "focus"
        watcher = ConfigWatcher(str(path), initial)

        data = json.loads(path.read_text(encoding="utf-8"))
        data["view_pin"] = None
        _write_config(path, data)
        _bump_mtime(path)

        outcome = watcher.poll()

        assert outcome.checked is True
        assert outcome.error is None
        assert outcome.applied == ("view_pin",)
        assert watcher.current.view_pin is None
