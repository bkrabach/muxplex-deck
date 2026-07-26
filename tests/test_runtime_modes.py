"""Runtime tests for both layout modes -- no hardware, no server, no I/O.

Drives `main._ActiveRuntime` with a fake `DeckDevice` (a 15-key/3x5/72px
Original-class deck, and an 8-key/2x4/120px Deck+-class deck) and a fake
`MuxplexClient` with canned responses, asserting:

- reduced mode: reserved-key presses page/cycle and NEVER connect; session
  presses connect the right session; the strip is never painted; rendered
  key images are the deck's real 72x72 (not an assumed 120).
- full mode (Stream Deck+): every key connects its session, dials drive
  paging/view cycling, and the strip is painted -- the pre-existing
  behavior, preserved.
"""

from __future__ import annotations

import io
import threading
from typing import cast

import pytest
from PIL import Image

from muxplex_deck.client import (
    Bell,
    MuxplexClient,
    ServerState,
    Session,
    Settings,
    View,
)
from muxplex_deck.device import DeckDevice, DialEventType
from muxplex_deck.interaction import PickerMode, ViewCycler
from muxplex_deck.layout import MODE_FULL, MODE_REDUCED
from muxplex_deck.main import _ActiveRuntime

# Fast debounce so VIEW-cycle commit tests don't wait the production 400ms.
_TEST_DEBOUNCE_SECONDS = 0.05
_WAIT_SECONDS = 5.0


class FakeDeck:
    """A `DeckDevice` fake with injectable capabilities and recorded I/O."""

    def __init__(
        self,
        *,
        key_count: int,
        key_layout: tuple[int, int],
        key_size: tuple[int, int],
        dial_count: int,
        is_touch: bool,
    ) -> None:
        self._key_count = key_count
        self._key_layout = key_layout
        self._key_size = key_size
        self._dial_count = dial_count
        self._is_touch = is_touch
        self._lock = threading.RLock()
        self.key_images: dict[int, bytes] = {}
        self.strip_paint_count = 0

    # capability surface
    def key_count(self) -> int:
        return self._key_count

    def key_layout(self) -> tuple[int, int]:
        return self._key_layout

    def dial_count(self) -> int:
        return self._dial_count

    def is_touch(self) -> bool:
        return self._is_touch

    def key_image_format(self) -> dict:
        return {
            "size": self._key_size,
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    def touchscreen_image_format(self) -> dict:
        return {
            "size": (800, 100),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    # device I/O (recorded)
    def set_key_image(self, key: int, image: bytes) -> None:
        self.key_images[key] = image

    def set_touchscreen_image(
        self,
        image: bytes,
        x_pos: int = 0,
        y_pos: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None:
        self.strip_paint_count += 1

    # lifecycle (unused by these tests, but part of the protocol surface)
    def open(self) -> None: ...
    def close(self) -> None: ...
    def reset(self) -> None: ...

    def is_open(self) -> bool:
        return True

    def connected(self) -> bool:
        return True

    def deck_type(self) -> str:
        return "Fake Deck"

    def get_serial_number(self) -> str:
        return "FAKE-0001"

    def get_firmware_version(self) -> str:
        return "0.0"

    def set_brightness(self, percent: float) -> None: ...
    def set_key_callback(self, callback: object) -> None: ...
    def set_dial_callback(self, callback: object) -> None: ...
    def set_touchscreen_callback(self, callback: object) -> None: ...

    def __enter__(self) -> None:
        self._lock.acquire()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._lock.release()


class FakeClient:
    """Canned muxplex responses + threading.Events for async assertions."""

    def __init__(self, sessions: list[Session], settings: Settings) -> None:
        self.sessions = sessions
        self.settings = settings
        self.active_session: str | None = None
        self.active_view = "all"
        self.connected_names: list[str] = []
        self.view_patches: list[str] = []
        self.connect_event = threading.Event()
        self.view_patch_event = threading.Event()

    def get_sessions(self) -> list[Session]:
        return list(self.sessions)

    def get_state(self) -> ServerState:
        return ServerState(
            active_session=self.active_session, active_view=self.active_view
        )

    def get_settings(self) -> Settings:
        return self.settings

    def connect_session(self, name: str) -> None:
        self.connected_names.append(name)
        self.connect_event.set()

    def set_active_view(self, view: str) -> None:
        self.view_patches.append(view)
        self.active_view = view
        self.view_patch_event.set()


def _make_sessions(count: int) -> list[Session]:
    bell = Bell(last_fired_at=None, seen_at=None, unseen_count=0)
    return [
        Session(name=f"session-{i:02d}", snapshot=f"line one\noutput {i}\n", bell=bell)
        for i in range(count)
    ]


def _make_runtime(deck: FakeDeck, client: FakeClient) -> _ActiveRuntime:
    ctx = _ActiveRuntime(
        deck=cast(DeckDevice, deck),  # satisfies DeckDevice structurally
        client=cast(MuxplexClient, client),
        hostname="test-server",
        sort_mode="server",  # deterministic ordering for assertions
    )
    ctx.view_cycler = ViewCycler(debounce_seconds=_TEST_DEBOUNCE_SECONDS)
    return ctx


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    return Image.open(io.BytesIO(image_bytes)).size


SETTINGS = Settings(
    views=(View(name="focus", sessions=frozenset({"session-00", "session-01"})),),
    hidden_sessions=frozenset(),
    sort_order="manual",
)


@pytest.fixture
def reduced() -> tuple[FakeDeck, FakeClient, _ActiveRuntime]:
    deck = FakeDeck(
        key_count=15,
        key_layout=(3, 5),
        key_size=(72, 72),
        dial_count=0,
        is_touch=False,
    )
    client = FakeClient(_make_sessions(20), SETTINGS)
    ctx = _make_runtime(deck, client)
    ctx.refresh()
    return deck, client, ctx


@pytest.fixture
def full() -> tuple[FakeDeck, FakeClient, _ActiveRuntime]:
    deck = FakeDeck(
        key_count=8,
        key_layout=(2, 4),
        key_size=(120, 120),
        dial_count=4,
        is_touch=True,
    )
    client = FakeClient(_make_sessions(20), SETTINGS)
    ctx = _make_runtime(deck, client)
    ctx.refresh()
    return deck, client, ctx


class TestReducedRuntime:
    """15-key Original-class deck: reserved keys, 12 session slots, no strip."""

    def test_plan_and_paging(self, reduced) -> None:
        _deck, _client, ctx = reduced
        assert ctx.plan.mode == MODE_REDUCED
        assert ctx.pager.page_size == 12
        assert ctx.pager.page_count == 2  # 20 sessions / 12 per page

    def test_all_keys_painted_at_real_72px_size(self, reduced) -> None:
        deck, _client, _ctx = reduced
        assert set(deck.key_images) == set(range(15))
        for image_bytes in deck.key_images.values():
            assert _image_size(image_bytes) == (72, 72)

    def test_strip_never_painted(self, reduced) -> None:
        deck, _client, _ctx = reduced
        assert deck.strip_paint_count == 0

    def test_session_key_press_connects_correct_session(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(1)  # first session slot -> page 1, slot 0
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-00"]
        assert ctx.active_session == "session-00"  # optimistic highlight

    def test_key_press_focuses_pwa_even_when_session_unchanged(
        self, reduced, monkeypatch
    ) -> None:
        """Regression: re-pressing the already-active session's key must still
        focus the PWA. `_do_connect` used to gate `focus.focus_app` on whether
        the press actually changed the active session, so pressing the SAME
        key twice only focused on the first press -- silently dropping the
        deck's "bring the window back" use case (e.g. after alt-tabbing away).
        """
        _deck, client, ctx = reduced
        focus_calls: list[str] = []
        monkeypatch.setattr(
            "muxplex_deck.main.focus.focus_app",
            lambda name: focus_calls.append(name),
        )
        ctx.focus_app_name = "Muxplex"

        ctx.handle_key(1)  # first press -> connects session-00 (changed)
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()

        ctx.handle_key(1)  # second press, SAME slot/session -> must still focus
        assert client.connect_event.wait(_WAIT_SECONDS)

        assert focus_calls == ["Muxplex", "Muxplex"], (
            "every explicit key press must focus the PWA, whether or not the "
            f"active session actually changed; got {focus_calls!r}"
        )

    def test_session_slot_after_reserved_key_maps_correctly(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(11)  # physical key 11 -> slot 9 (10 is reserved PREV)
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-09"]

    def test_next_prev_keys_page_and_never_connect(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(14)  # NEXT
        assert ctx.pager.page == 2
        ctx.handle_key(14)  # NEXT again -- clamped at last page
        assert ctx.pager.page == 2
        ctx.handle_key(10)  # PREV
        assert ctx.pager.page == 1
        ctx.handle_key(10)  # PREV again -- clamped at first page
        assert ctx.pager.page == 1
        assert client.connected_names == []

    def test_second_page_session_press_connects_paged_session(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(14)  # NEXT -> page 2 (sessions 12..19)
        ctx.repaint()
        ctx.handle_key(1)  # slot 0 of page 2
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-12"]

    def test_view_key_opens_picker_and_never_connects(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(0)  # VIEW tap -> opens the view picker (no cycle, no PATCH)
        assert ctx.picker.mode == PickerMode.VIEW
        assert ctx.picker.window_start == 0
        assert client.view_patches == []
        assert client.connected_names == []

    def test_page_resets_on_view_change(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(14)  # NEXT -> page 2
        assert ctx.pager.page == 2
        client.active_view = "focus"  # server-side view change (e.g. the PWA)
        ctx.refresh()
        assert ctx.pager.page == 1


class TestReducedViewPicker:
    """Reduced-layout paged view picker: VIEW opens, BACK cancels, tap selects."""

    def _open_picker(self, ctx: _ActiveRuntime) -> None:
        ctx.handle_key(0)
        assert ctx.picker.mode == PickerMode.VIEW

    def test_select_view_patches_global_active_view_and_exits(self, reduced) -> None:
        _deck, client, ctx = reduced
        self._open_picker(ctx)
        # options are ["all", "focus"]; key 2 is session slot 1 -> "focus"
        ctx.handle_key(2)
        assert client.view_patches == ["focus"]  # PATCH /api/state active_view
        assert ctx.picker.mode == PickerMode.NONE
        assert client.connected_names == []  # a picker tap never connects

    def test_back_key_cancels_without_patch(self, reduced) -> None:
        _deck, client, ctx = reduced
        self._open_picker(ctx)
        ctx.handle_key(0)  # VIEW key is BACK while the picker is open
        assert ctx.picker.mode == PickerMode.NONE
        assert client.view_patches == []
        assert client.connected_names == []

    def test_picker_slots_show_views_with_active_highlighted(self, reduced) -> None:
        _deck, _client, ctx = reduced
        self._open_picker(ctx)
        # session-slot keys 1, 2, 3 carry the three options: "all", the one
        # named view, and the trailing "hidden" pseudo-view; "all" is active
        assert ctx.last_key_state[1] == ("picker", "all", True)
        assert ctx.last_key_state[2] == ("picker", "focus", False)
        assert ctx.last_key_state[3] == ("picker", "hidden", False)
        assert ctx.last_key_state[4] is None  # empty option slot
        # reserved keys repurposed: BACK on the VIEW key, pagers inert (1 page)
        assert ctx.last_key_state[0] == ("control", "VIEW", "< BACK", "")
        assert ctx.last_key_state[10] == ("control", "", "< PREV", "")
        assert ctx.last_key_state[14] == ("control", "", "NEXT >", "")

    def test_hidden_pseudo_view_selectable_like_all(self, reduced) -> None:
        """Regression: "hidden" is a reserved pseudo-view exactly like "all" --
        never a member of settings.views, but reachable through the same
        picker. Selecting it PATCHes active_view="hidden", same as any other
        option.
        """
        _deck, client, ctx = reduced
        self._open_picker(ctx)
        ctx.handle_key(3)  # slot 2 -> "hidden" (see test above)
        assert client.view_patches == ["hidden"]
        assert ctx.picker.mode == PickerMode.NONE
        assert client.connected_names == []  # a picker tap never connects

    def test_empty_option_slot_tap_ignored_keeps_picker_open(self, reduced) -> None:
        _deck, client, ctx = reduced
        self._open_picker(ctx)
        ctx.handle_key(5)  # slot 4 -- past the 2 options
        assert ctx.picker.mode == PickerMode.VIEW
        assert client.view_patches == []
        assert client.connected_names == []

    def test_poll_does_not_repaint_grid_over_picker(self, reduced) -> None:
        _deck, _client, ctx = reduced
        self._open_picker(ctx)
        ctx.refresh()  # a poll tick while the picker is open
        assert ctx.picker.mode == PickerMode.VIEW
        assert ctx.last_key_state[1] == ("picker", "all", True)  # still the picker

    def test_grid_restored_after_cancel(self, reduced) -> None:
        deck, _client, ctx = reduced
        before = dict(deck.key_images)
        self._open_picker(ctx)
        assert deck.key_images != before  # picker painted over the grid
        ctx.handle_key(0)  # BACK
        assert deck.key_images == before  # exact same session tiles repainted


MANY_VIEWS_SETTINGS = Settings(
    # 15 named views + the implicit "all" + the implicit "hidden" = 17
    # options -> 2 picker pages of 12.
    views=tuple(View(name=f"view-{i:02d}", sessions=frozenset()) for i in range(15)),
    hidden_sessions=frozenset(),
    sort_order="manual",
)


@pytest.fixture
def reduced_many_views() -> tuple[FakeDeck, FakeClient, _ActiveRuntime]:
    deck = FakeDeck(
        key_count=15,
        key_layout=(3, 5),
        key_size=(72, 72),
        dial_count=0,
        is_touch=False,
    )
    client = FakeClient(_make_sessions(3), MANY_VIEWS_SETTINGS)
    ctx = _make_runtime(deck, client)
    ctx.refresh()
    return deck, client, ctx


class TestReducedViewPickerPaging:
    """>12 views: PREV/NEXT page the picker window, clamped, reset on re-entry."""

    def test_next_pages_and_second_page_maps_correct_names(
        self, reduced_many_views
    ) -> None:
        _deck, client, ctx = reduced_many_views
        ctx.handle_key(0)  # open picker: window 0 shows options[0:12]
        assert ctx.last_key_state[1] == ("picker", "all", True)
        ctx.handle_key(14)  # NEXT -> window 12 shows options[12:17]
        assert ctx.picker.window_start == 12
        # options = ["all"] + view-00..view-14 + ["hidden"]; options[12] == "view-11"
        assert ctx.last_key_state[1] == ("picker", "view-11", False)
        assert ctx.last_key_state[4] == ("picker", "view-14", False)  # slot 3
        # slot 4 -- options[16] == "hidden", the trailing pseudo-view
        assert ctx.last_key_state[5] == ("picker", "hidden", False)
        # pagers show the picker's own page footer on page 2 of 2
        assert ctx.last_key_state[14] == ("control", "", "NEXT >", "p2/2")
        # slot tap on page 2 selects the right (windowed) view
        ctx.handle_key(2)  # slot 1 -> options[13] == "view-12"
        assert client.view_patches == ["view-12"]
        assert ctx.picker.mode == PickerMode.NONE

    def test_paging_clamps_at_both_ends(self, reduced_many_views) -> None:
        _deck, client, ctx = reduced_many_views
        ctx.handle_key(0)
        ctx.handle_key(10)  # PREV on first page -- clamped
        assert ctx.picker.window_start == 0
        ctx.handle_key(14)  # NEXT -> second page
        ctx.handle_key(14)  # NEXT again -- clamped at last page
        assert ctx.picker.window_start == 12
        assert ctx.picker.mode == PickerMode.VIEW
        assert client.view_patches == []

    def test_reentering_picker_resets_to_first_page(self, reduced_many_views) -> None:
        _deck, _client, ctx = reduced_many_views
        ctx.handle_key(0)
        ctx.handle_key(14)  # NEXT -> window 12
        ctx.handle_key(0)  # BACK -> closed
        ctx.handle_key(0)  # reopen
        assert ctx.picker.window_start == 0
        assert ctx.last_key_state[1] == ("picker", "all", True)


class TestFullRuntime:
    """Stream Deck+-class deck: the pre-existing behavior, preserved."""

    def test_plan_all_keys_are_sessions(self, full) -> None:
        _deck, _client, ctx = full
        assert ctx.plan.mode == MODE_FULL
        assert ctx.plan.session_slots == tuple(range(8))
        assert ctx.pager.page_size == 8
        assert ctx.pager.page_count == 3  # 20 sessions / 8 per page

    def test_keys_painted_at_real_120px_size_and_strip_painted(self, full) -> None:
        deck, _client, _ctx = full
        assert set(deck.key_images) == set(range(8))
        for image_bytes in deck.key_images.values():
            assert _image_size(image_bytes) == (120, 120)
        assert deck.strip_paint_count > 0

    def test_every_key_connects_its_session(self, full) -> None:
        _deck, client, ctx = full
        ctx.handle_key(3)
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-03"]

    def test_page_dial_turns_and_clamps(self, full) -> None:
        _deck, client, ctx = full
        ctx.handle_page_dial(DialEventType.TURN, 1)
        assert ctx.pager.page == 2
        ctx.handle_page_dial(DialEventType.TURN, 5)
        assert ctx.pager.page == 3  # clamped at last page
        ctx.handle_page_dial(DialEventType.TURN, -10)
        assert ctx.pager.page == 1  # clamped at first page
        assert client.connected_names == []

    def test_view_dial_turn_commits_debounced_patch(self, full) -> None:
        _deck, client, ctx = full
        ctx.handle_view_dial(DialEventType.TURN, 1)
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["focus"]

    def test_page_resets_on_view_change(self, full) -> None:
        _deck, client, ctx = full
        ctx.handle_page_dial(DialEventType.TURN, 1)
        assert ctx.pager.page == 2
        client.active_view = "focus"
        ctx.refresh()
        assert ctx.pager.page == 1
