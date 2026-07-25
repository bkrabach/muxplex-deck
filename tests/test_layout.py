"""Pure layout-planning tests -- no hardware, no server, no I/O.

Fake capability dicts for the hardware-verified 15-key Original (3x5, no
dials, no touch) and the Stream Deck+ (2x4, 4 dials, touch strip), plus the
other real no-dial grids (XL 4x8, Mini 2x3) and the documented edge cases,
exercise `plan_layout` / `classify_key` and the paging math (`Pager`) the
plan feeds.
"""

from __future__ import annotations

from muxplex_deck.interaction import Pager
from muxplex_deck.layout import (
    KEY_NEXT,
    KEY_PREV,
    KEY_SESSION,
    KEY_VIEW,
    MODE_FULL,
    MODE_REDUCED,
    classify_key,
    plan_layout,
)

CAPS_ORIGINAL_15 = {
    "key_count": 15,
    "key_rows": 3,
    "key_cols": 5,
    "dial_count": 0,
    "is_touch": False,
}
CAPS_PLUS = {
    "key_count": 8,
    "key_rows": 2,
    "key_cols": 4,
    "dial_count": 4,
    "is_touch": True,
}
CAPS_XL = {
    "key_count": 32,
    "key_rows": 4,
    "key_cols": 8,
    "dial_count": 0,
    "is_touch": False,
}
CAPS_MINI = {
    "key_count": 6,
    "key_rows": 2,
    "key_cols": 3,
    "dial_count": 0,
    "is_touch": False,
}


class TestReducedMode:
    """15-key Original: 3 reserved control keys, 12 session slots."""

    def test_mode_and_reserved_indices(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15)
        assert plan.mode == MODE_REDUCED
        assert plan.view_key == 0  # top-left
        assert plan.prev_key == 10  # bottom-left = (rows-1)*cols = 2*5
        assert plan.next_key == 14  # bottom-right = rows*cols - 1

    def test_session_slots_skip_reserved_in_reading_order(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15)
        assert plan.session_slots == (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13)
        assert plan.sessions_per_page == 12

    def test_no_dials_no_strip(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15)
        assert plan.use_dials is False
        assert plan.use_strip is False

    def test_classify_reserved_keys(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15)
        assert classify_key(plan, 0) == (KEY_VIEW, None)
        assert classify_key(plan, 10) == (KEY_PREV, None)
        assert classify_key(plan, 14) == (KEY_NEXT, None)

    def test_classify_session_keys_map_to_slot_positions(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15)
        assert classify_key(plan, 1) == (KEY_SESSION, 0)
        assert classify_key(plan, 9) == (KEY_SESSION, 8)
        # key 11 comes after the reserved PREV key at 10 -> slot 9
        assert classify_key(plan, 11) == (KEY_SESSION, 9)
        assert classify_key(plan, 13) == (KEY_SESSION, 11)

    def test_other_no_dial_grids_use_same_corner_math(self) -> None:
        xl = plan_layout(CAPS_XL)
        assert (xl.view_key, xl.prev_key, xl.next_key) == (0, 24, 31)
        assert xl.sessions_per_page == 29

        mini = plan_layout(CAPS_MINI)
        assert (mini.view_key, mini.prev_key, mini.next_key) == (0, 3, 5)
        assert mini.session_slots == (1, 2, 4)
        assert mini.sessions_per_page == 3


class TestFullMode:
    """Stream Deck+: pre-existing behavior -- every key a session tile."""

    def test_mode_and_slots(self) -> None:
        plan = plan_layout(CAPS_PLUS)
        assert plan.mode == MODE_FULL
        assert plan.view_key is None
        assert plan.prev_key is None
        assert plan.next_key is None
        assert plan.session_slots == tuple(range(8))
        assert plan.sessions_per_page == 8

    def test_dials_and_strip_active(self) -> None:
        plan = plan_layout(CAPS_PLUS)
        assert plan.use_dials is True
        assert plan.use_strip is True

    def test_every_key_classifies_as_its_own_session_slot(self) -> None:
        plan = plan_layout(CAPS_PLUS)
        for key in range(8):
            assert classify_key(plan, key) == (KEY_SESSION, key)


class TestEdgeCases:
    def test_dials_but_no_touch_is_reduced_with_unassigned_dials(self) -> None:
        caps = {**CAPS_PLUS, "is_touch": False}
        plan = plan_layout(caps)
        assert plan.mode == MODE_REDUCED
        assert plan.use_dials is False
        assert plan.use_strip is False
        assert (plan.view_key, plan.prev_key, plan.next_key) == (0, 4, 7)

    def test_touch_but_one_dial_is_reduced_but_keeps_strip(self) -> None:
        caps = {**CAPS_PLUS, "dial_count": 1}
        plan = plan_layout(caps)
        assert plan.mode == MODE_REDUCED
        assert plan.use_dials is False
        assert plan.use_strip is True  # strip still shows the status headline

    def test_too_few_keys_falls_back_to_all_session_tiles(self) -> None:
        caps = {
            "key_count": 3,
            "key_rows": 1,
            "key_cols": 3,
            "dial_count": 0,
            "is_touch": False,
        }
        plan = plan_layout(caps)
        assert plan.mode == MODE_REDUCED
        assert plan.view_key is None and plan.prev_key is None
        assert plan.session_slots == (0, 1, 2)
        assert plan.sessions_per_page == 3

    def test_single_row_corner_collision_falls_back(self) -> None:
        # rows=1: top-left == bottom-left -- corners collide.
        caps = {
            "key_count": 6,
            "key_rows": 1,
            "key_cols": 6,
            "dial_count": 0,
            "is_touch": False,
        }
        plan = plan_layout(caps)
        assert plan.view_key is None
        assert plan.session_slots == tuple(range(6))


class TestPagingMath:
    """The paging semantics the reserved PREV/NEXT keys drive (via `Pager`)."""

    def test_page_count_for_15_key_deck(self) -> None:
        pager = Pager(page_size=plan_layout(CAPS_ORIGINAL_15).sessions_per_page)
        pager.set_item_count(65)
        assert pager.page_count == 6  # ceil(65 / 12)

    def test_turn_clamps_at_both_ends(self) -> None:
        pager = Pager(page_size=12)
        pager.set_item_count(30)  # 3 pages
        assert pager.turn(-1) == 1  # clamped at first page
        assert pager.turn(1) == 2
        assert pager.turn(5) == 3  # clamped at last page
        assert pager.turn(1) == 3

    def test_reset_returns_to_page_one(self) -> None:
        pager = Pager(page_size=12)
        pager.set_item_count(30)
        pager.turn(2)
        pager.reset()
        assert pager.page == 1

    def test_shrinking_item_count_clamps_current_page(self) -> None:
        pager = Pager(page_size=12)
        pager.set_item_count(30)
        pager.turn(2)
        pager.set_item_count(5)  # now a single page
        assert pager.page == 1
        assert pager.page_count == 1
