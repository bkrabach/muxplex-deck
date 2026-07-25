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
from muxplex_deck.interaction import ViewCycler
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
        hostname="spark-1",
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

    def test_view_key_cycles_view_and_never_connects(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(0)  # VIEW tap -> next view after "all" is "focus"
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["focus"]
        assert client.connected_names == []

    def test_page_resets_on_view_change(self, reduced) -> None:
        _deck, client, ctx = reduced
        ctx.handle_key(14)  # NEXT -> page 2
        assert ctx.pager.page == 2
        client.active_view = "focus"  # server-side view change (e.g. the PWA)
        ctx.refresh()
        assert ctx.pager.page == 1


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
