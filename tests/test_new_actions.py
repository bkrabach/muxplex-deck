"""Runtime tests for the Tier-2 catalog actions -- design test requirements

#10-17 from docs/CONTROL_MAPPING_DESIGN.md §10: kind correctness is covered
in test_controls.py; this module covers the RUNTIME behavior of each new
action end-to-end through `_ActiveRuntime`, using the same `FakeDeck`/
`FakeClient` fakes as test_runtime_modes.py (no hardware, no server, no
real threads left dangling -- every backgrounded action is awaited via its
own `threading.Event` or joined explicitly).
"""

from __future__ import annotations

import threading
from typing import cast

import pytest
from muxplex_client import MuxplexClient, Settings, View
from test_runtime_modes import FakeClient, FakeDeck, _make_sessions

from muxplex_deck.device import DeckDevice
from muxplex_deck.interaction import ViewCycler
from muxplex_deck.main import (
    BRIGHTNESS_FLOOR_PERCENT,
    FULL_BRIGHTNESS_PERCENT,
    _ActiveRuntime,
)

_TEST_DEBOUNCE_SECONDS = 0.05
_WAIT_SECONDS = 5.0

SETTINGS = Settings(
    views=(View(name="focus", sessions=frozenset({"session-00", "session-01"})),),
    hidden_sessions=frozenset(),
    sort_order="manual",
)


def _make_runtime_with_controls(
    deck: FakeDeck, client: FakeClient, controls: dict[str, str]
) -> _ActiveRuntime:
    ctx = _ActiveRuntime(
        deck=cast(DeckDevice, deck),
        client=cast(MuxplexClient, client),
        hostname="test-server",
        sort_mode="server",
        controls=controls,
    )
    ctx.view_cycler = ViewCycler(debounce_seconds=_TEST_DEBOUNCE_SECONDS)
    return ctx


@pytest.fixture
def reduced_remapped():
    """15-key Original, with key.11 (a normally-session key) remapped per-test.

    Callers pass `controls` via the fixture factory pattern below instead --
    see `_reduced_with` for the actual per-test construction, since fixture
    params can't easily vary per test otherwise.
    """


def _reduced_with(controls: dict[str, str], *, session_count: int = 20):
    deck = FakeDeck(
        key_count=15, key_layout=(3, 5), key_size=(72, 72), dial_count=0, is_touch=False
    )
    client = FakeClient(_make_sessions(session_count), SETTINGS)
    ctx = _make_runtime_with_controls(deck, client, controls)
    ctx.refresh()
    return deck, client, ctx


def _full_with(controls: dict[str, str], *, session_count: int = 20):
    deck = FakeDeck(
        key_count=8, key_layout=(2, 4), key_size=(120, 120), dial_count=4, is_touch=True
    )
    client = FakeClient(_make_sessions(session_count), SETTINGS)
    ctx = _make_runtime_with_controls(deck, client, controls)
    ctx.refresh()
    return deck, client, ctx


class TestViewPrevNext:
    """§2.3 #1: the biggest miss the low-hanging-fruit pass found -- a REDUCED

    deck previously had NO way to step views at all outside the picker.
    """

    def test_view_next_steps_and_commits(self) -> None:
        _deck, client, ctx = _reduced_with({"key.11": "view_next"})
        ctx.handle_key(11)
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["focus"]  # "all" -> "focus" (next in cycle)

    def test_view_prev_steps_and_commits(self) -> None:
        _deck, client, ctx = _reduced_with({"key.11": "view_prev"})
        ctx.handle_key(11)
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["hidden"]  # "all" -> "hidden" (prev, ring)

    def test_view_next_never_connects_a_session(self) -> None:
        _deck, client, ctx = _reduced_with({"key.11": "view_next"})
        ctx.handle_key(11)
        client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.connected_names == []


class TestPageFirstLast:
    def test_page_last_jumps_to_final_page(self) -> None:
        _deck, _client, ctx = _reduced_with({"key.11": "page_last"})
        assert ctx.pager.page_count == 2  # 20 sessions / 12 per page
        ctx.handle_key(11)
        assert ctx.pager.page == 2

    def test_page_first_jumps_back_to_page_one(self) -> None:
        _deck, _client, ctx = _reduced_with({"key.11": "page_first"})
        ctx.pager.turn(1)
        assert ctx.pager.page == 2
        ctx.handle_key(11)
        assert ctx.pager.page == 1


class TestViewAll:
    def test_view_all_jumps_immediately_no_debounce(self) -> None:
        _deck, client, ctx = _reduced_with({"key.11": "view_all"})
        # Move off "all" first via the normal debounced path isn't needed --
        # from "all", pressing view_all still triggers exactly one commit.
        ctx.handle_key(11)
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["all"]


class TestRefreshNow:
    def test_refresh_now_runs_to_completion_on_a_background_thread(self) -> None:
        _deck, client, ctx = _reduced_with({"key.11": "refresh_now"})
        before = len(client.connected_names)
        ctx.handle_key(11)
        # refresh() calls client.sessions()/.state()/.settings() -- give the
        # background thread a moment, then confirm no exception was raised
        # (a failure would have logged, not crashed) and state is coherent.
        import time

        time.sleep(0.2)
        assert len(client.connected_names) == before  # never connects anything
        assert ctx.active_view == "all"

    def test_refresh_now_does_not_disturb_poll_loop_contract(self) -> None:
        """A manual refresh and a concurrent `ctx.refresh()` call are both

        safe -- design test requirement #15.
        """
        _deck, _client, ctx = _reduced_with({"key.11": "refresh_now"})
        threads = [threading.Thread(target=ctx.refresh) for _ in range(3)]
        for t in threads:
            t.start()
        ctx.handle_key(11)  # a third concurrent path via _dispatch
        for t in threads:
            t.join(timeout=_WAIT_SECONDS)
        assert all(not t.is_alive() for t in threads)


class TestFocusAppAction:
    """Backlog item 3: focus-grabbing moved server-side -- a key bound to

    `focus_app` now fires `client.raise_focus()` (POST /api/focus on the
    configured server) in a background thread, via
    `main._raise_focus_best_effort`, instead of the deleted local
    `focus.focus_app()`.
    """

    def test_focus_app_bound_key_triggers_client_raise_focus_in_background(
        self,
    ) -> None:
        done = threading.Event()
        _deck, client, ctx = _reduced_with({"key.11": "focus_app"})

        def recording_raise_focus() -> None:
            client.raise_focus_calls += 1
            done.set()

        client.raise_focus = recording_raise_focus  # type: ignore[method-assign]
        ctx.handle_key(11)
        assert done.wait(_WAIT_SECONDS)
        assert client.raise_focus_calls == 1

    def test_focus_app_swallows_a_raise_focus_failure(self) -> None:
        """`_raise_focus_best_effort` must never propagate a MuxplexError --

        same never-raise contract the deleted local `focus.focus_app` had.
        """
        from muxplex_client import ApiError

        done = threading.Event()
        _deck, client, ctx = _reduced_with({"key.11": "focus_app"})

        def failing_raise_focus() -> None:
            done.set()
            raise ApiError(409, "focus_app is not set")

        client.raise_focus = failing_raise_focus  # type: ignore[method-assign]
        ctx.handle_key(11)  # must not raise out of the HID callback thread
        assert done.wait(_WAIT_SECONDS)

    def test_focus_app_never_connects_a_session(self) -> None:
        _deck, client, ctx = _reduced_with({"key.11": "focus_app"})
        client.raise_focus = lambda: None  # type: ignore[method-assign]
        ctx.handle_key(11)
        assert client.connected_names == []

    def test_focus_module_is_gone(self) -> None:
        """Import guard: `muxplex_deck.focus` was deleted in full (backlog

        item 3 -- focus-grabbing moved server-side). If a future change
        ever re-adds it, this failing test makes the revival visible in
        review rather than silently reintroducing the duplicated,
        Windows-specific implementation this item removed.
        """
        import importlib

        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("muxplex_deck.focus")


class TestToggleLast:
    def test_toggles_between_two_sessions(self) -> None:
        _deck, client, ctx = _reduced_with({"key.13": "toggle_last"})
        ctx.handle_key(1)  # connect session-00
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()
        ctx.handle_key(2)  # connect session-01 (session-00 becomes "previous")
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()

        ctx.handle_key(13)  # TOGGLE -> back to session-00
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names[-1] == "session-00"

    def test_no_previous_session_is_a_no_op(self) -> None:
        _deck, client, ctx = _reduced_with({"key.13": "toggle_last"})
        ctx.handle_key(13)  # never connected anything yet
        assert client.connected_names == []

    def test_dead_session_guard_no_failed_connect(self) -> None:
        """Toggling to a session that no longer exists logs and no-ops --

        design test requirement #13.
        """
        _deck, client, ctx = _reduced_with({"key.13": "toggle_last"}, session_count=3)
        ctx.handle_key(1)  # connect session-00
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()
        ctx.handle_key(2)  # connect session-01 -- session-00 now "previous"
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()

        # session-00 disappears from the server's list.
        client._sessions = [s for s in client._sessions if s.name != "session-00"]
        ctx.refresh()

        ctx.handle_key(13)  # TOGGLE -> session-00 no longer exists
        assert not client.connect_event.wait(0.2)
        assert "session-00" not in client.connected_names[-1:]

    def test_server_side_switch_updates_previous_session(self) -> None:
        """A session change observed via `_process` (e.g. the PWA switched)

        updates `previous_session`, not just local key presses -- design
        test requirement #14.
        """
        _deck, client, ctx = _reduced_with({"key.13": "toggle_last"})
        ctx.handle_key(1)  # connect session-00 locally
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()

        client.active_session = "session-05"  # server-side switch (the PWA)
        ctx.refresh()
        assert ctx.active_session == "session-05"
        assert ctx.previous_session == "session-00"

        ctx.handle_key(13)  # TOGGLE -> back to session-00
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names[-1] == "session-00"


class TestBrightness:
    def test_repeated_brightness_down_floors_at_10_never_0(self) -> None:
        _deck, _client, ctx = _reduced_with({"key.11": "brightness_down"})
        for _ in range(20):
            ctx.handle_key(11)
        assert ctx.brightness == BRIGHTNESS_FLOOR_PERCENT
        assert ctx.brightness > 0

    def test_repeated_brightness_up_caps_at_100(self) -> None:
        _deck, _client, ctx = _reduced_with({"key.11": "brightness_up"})
        ctx.brightness = 95
        for _ in range(5):
            ctx.handle_key(11)
        assert ctx.brightness == 100

    def test_brightness_cycle_clamps_both_ends(self) -> None:
        _deck, _client, ctx = _full_with({"dial.2.turn": "brightness_cycle"})
        ctx.handle_dial_turn(2, "brightness_cycle", -50)
        assert ctx.brightness == BRIGHTNESS_FLOOR_PERCENT
        ctx.handle_dial_turn(2, "brightness_cycle", 50)
        assert ctx.brightness == 100

    def test_brightness_actually_calls_set_brightness(self) -> None:
        deck, _client, ctx = _reduced_with({"key.11": "brightness_down"})
        calls: list[float] = []
        deck.set_brightness = lambda percent: calls.append(percent)  # type: ignore[method-assign]
        ctx.handle_key(11)
        assert calls == [90]

    def test_brightness_is_session_local_never_persisted(self) -> None:
        """A fresh `_ActiveRuntime` (simulating a reconnect) always starts

        at full brightness, regardless of a previous session's dimming --
        design test requirement #12. No config write ever happens: dimming
        only mutates the in-memory `brightness` field.
        """
        deck, client, ctx = _reduced_with({"key.11": "brightness_down"})
        ctx.handle_key(11)
        assert ctx.brightness == FULL_BRIGHTNESS_PERCENT - 10

        # A new connection (reconnect) is a fresh _ActiveRuntime -- exactly
        # what `_run_active` constructs on every bring-up.
        fresh_ctx = _make_runtime_with_controls(
            deck, client, {"key.11": "brightness_down"}
        )
        assert fresh_ctx.brightness == FULL_BRIGHTNESS_PERCENT


class TestReclaimedDialsOnDeckPlus:
    """§4.4: the Deck+'s dials 2/3 are dead by default -- reclaimable via config."""

    def test_dial_2_turn_reclaimed_for_paging(self) -> None:
        _deck, _client, ctx = _full_with({"dial.2.turn": "page_cycle"})
        ctx.handle_dial_turn(2, "page_cycle", 1)
        assert ctx.pager.page == 2

    def test_dial_3_push_reclaimed_for_view_all(self) -> None:
        _deck, client, ctx = _full_with({"dial.3.push": "view_all"})
        ctx.handle_dial_push(3, "view_all")
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["all"]

    def test_unbound_dial_is_still_a_pure_no_op(self) -> None:
        _deck, client, ctx = _full_with({})
        ctx.handle_dial_turn(3, "none", 5)
        ctx.handle_dial_push(3, "none")
        assert client.connected_names == []
        assert client.view_patches == []


class TestNewActionsInertInPickerMode:
    """§7 / §2.6: every action outside the five-name default-allow set is

    ACTION_IGNORE while a picker is open -- design test requirement #17.
    """

    @pytest.mark.parametrize(
        "action",
        [
            "view_all",
            "page_first",
            "page_last",
            "view_prev",
            "view_next",
            "focus_app",
            "refresh_now",
            "toggle_last",
            "brightness_up",
            "brightness_down",
            "none",
        ],
    )
    def test_action_bound_to_a_session_slot_is_ignored_while_picker_open(
        self, action: str
    ) -> None:
        from muxplex_deck.interaction import ACTION_IGNORE, handle_picker_key

        result = handle_picker_key(
            kind=action, slot=0, options=["all", "focus"], window_start=0, page_size=12
        )
        assert result.action == ACTION_IGNORE
