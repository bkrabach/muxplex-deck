"""Pure tests for the reduced-layout view picker dispatch -- no I/O at all.

`interaction.handle_picker_key` is the whole decision table for a key press
while the key-driven view picker is open; these tests exercise it directly
with plain lists (no deck, no client, no threads). The runtime wiring on
top of it is covered by `test_runtime_modes.py`.
"""

from __future__ import annotations

from muxplex_deck.interaction import (
    ACTION_CANCEL,
    ACTION_IGNORE,
    ACTION_PAGE,
    ACTION_SELECT,
    clamp_window_start,
    handle_picker_key,
)

# `interaction.handle_picker_key`'s `kind` parameter is now the pressed
# key's resolved catalog action name (see `layout.classify_key`), not the
# old KEY_VIEW/KEY_PREV/KEY_NEXT/KEY_SESSION constants.
KEY_VIEW = "view_picker"
KEY_PREV = "page_prev"
KEY_NEXT = "page_next"
KEY_SESSION = "session"

# 16 options across a 12-per-page window -> pages start at 0 and 12.
OPTIONS = ["all"] + [f"view-{i:02d}" for i in range(15)]
PAGE_SIZE = 12


class TestHandlePickerKey:
    def test_view_key_cancels(self) -> None:
        result = handle_picker_key(
            kind=KEY_VIEW,
            slot=None,
            options=OPTIONS,
            window_start=0,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_CANCEL
        assert result.view is None

    def test_slot_selects_exact_view_name(self) -> None:
        result = handle_picker_key(
            kind=KEY_SESSION,
            slot=1,
            options=OPTIONS,
            window_start=0,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_SELECT
        assert result.view == "view-00"

    def test_slot_on_second_page_maps_windowed_name(self) -> None:
        result = handle_picker_key(
            kind=KEY_SESSION,
            slot=1,
            options=OPTIONS,
            window_start=12,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_SELECT
        assert result.view == "view-12"  # OPTIONS[13]

    def test_empty_slot_past_options_ignored(self) -> None:
        result = handle_picker_key(
            kind=KEY_SESSION,
            slot=5,
            options=OPTIONS,
            window_start=12,  # window holds OPTIONS[12:16] -> slots 0..3 only
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_IGNORE
        assert result.view is None

    def test_unknown_key_slot_none_ignored(self) -> None:
        result = handle_picker_key(
            kind=KEY_SESSION,
            slot=None,
            options=OPTIONS,
            window_start=0,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_IGNORE

    def test_next_pages_forward(self) -> None:
        result = handle_picker_key(
            kind=KEY_NEXT,
            slot=None,
            options=OPTIONS,
            window_start=0,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_PAGE
        assert result.window_start == 12

    def test_next_clamps_at_last_page(self) -> None:
        result = handle_picker_key(
            kind=KEY_NEXT,
            slot=None,
            options=OPTIONS,
            window_start=12,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_PAGE
        assert result.window_start == 12

    def test_prev_pages_back_and_clamps_at_zero(self) -> None:
        back = handle_picker_key(
            kind=KEY_PREV,
            slot=None,
            options=OPTIONS,
            window_start=12,
            page_size=PAGE_SIZE,
        )
        assert (back.action, back.window_start) == (ACTION_PAGE, 0)
        clamped = handle_picker_key(
            kind=KEY_PREV,
            slot=None,
            options=OPTIONS,
            window_start=0,
            page_size=PAGE_SIZE,
        )
        assert (clamped.action, clamped.window_start) == (ACTION_PAGE, 0)

    def test_single_page_paging_is_inert(self) -> None:
        options = ["all", "focus"]
        for kind in (KEY_PREV, KEY_NEXT):
            result = handle_picker_key(
                kind=kind,
                slot=None,
                options=options,
                window_start=0,
                page_size=PAGE_SIZE,
            )
            assert (result.action, result.window_start) == (ACTION_PAGE, 0)

    def test_stale_window_reclamped_before_dispatch(self) -> None:
        # The option list shrank since the window was set (a view deleted):
        # a select against the stale window must map within the re-clamped one.
        result = handle_picker_key(
            kind=KEY_SESSION,
            slot=0,
            options=["all", "focus"],
            window_start=12,
            page_size=PAGE_SIZE,
        )
        assert result.action == ACTION_SELECT
        assert result.view == "all"

    def test_empty_options_never_crash(self) -> None:
        for kind, slot in ((KEY_SESSION, 0), (KEY_NEXT, None), (KEY_VIEW, None)):
            result = handle_picker_key(
                kind=kind, slot=slot, options=[], window_start=0, page_size=PAGE_SIZE
            )
            assert result.action in (ACTION_IGNORE, ACTION_PAGE, ACTION_CANCEL)


class TestClampWindowStart:
    def test_clamps_negative_to_zero(self) -> None:
        assert clamp_window_start(-12, total=16, page_size=12) == 0

    def test_clamps_past_end_to_last_page_start(self) -> None:
        assert clamp_window_start(24, total=16, page_size=12) == 12

    def test_single_page_always_zero(self) -> None:
        assert clamp_window_start(12, total=5, page_size=12) == 0

    def test_exact_multiple_boundary(self) -> None:
        # 24 options / 12 per page -> last page starts at 12, not 24.
        assert clamp_window_start(24, total=24, page_size=12) == 12
