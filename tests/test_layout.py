"""Pure layout-planning tests -- no hardware, no server, no I/O.

Fake capability dicts for the hardware-verified 15-key Original (3 rows x 5
cols, no dials, no touch) and the Stream Deck+ (2 rows x 4 cols, 4 dials,
touch strip), plus the other real no-dial grids (XL 4x8, Mini 2 rows x 3
cols) and the documented edge cases, exercise `plan_layout` / `classify_key`
and the paging math (`Pager`) the plan feeds.

All `key_rows`/`key_cols` values here follow the `key_layout()` tuple order
verified in `layout.read_capabilities`: `rows, cols = deck.key_layout()` --
NOT `(cols, rows)`. The 15-key Original's own capability dict below
(`key_rows=3, key_cols=5`) is the cross-check: it's a 3-row, 5-column grid,
matching every existing REDUCED-mode assertion in this file.

`TestZeroConfigEquivalence` is the strongest guarantee in
docs/CONTROL_MAPPING_DESIGN.md: `plan_layout(caps, {})` (or the default,
no-overrides call) must classify every key/dial exactly as the pre-mapping
implementation did. Expected results are literal tables, not derived from
the new code -- pinning v0.9.5 behavior so a regression here means the
zero-config promise broke.
"""

from __future__ import annotations

from muxplex_deck.interaction import Pager
from muxplex_deck.layout import (
    MODE_FULL,
    MODE_REDUCED,
    classify_key,
    default_bindings,
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
        assert classify_key(plan, 0) == ("view_picker", None)
        assert classify_key(plan, 10) == ("page_prev", None)
        assert classify_key(plan, 14) == ("page_next", None)

    def test_classify_session_keys_map_to_slot_positions(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15)
        assert classify_key(plan, 1) == ("session", 0)
        assert classify_key(plan, 9) == ("session", 8)
        # key 11 comes after the reserved PREV key at 10 -> slot 9
        assert classify_key(plan, 11) == ("session", 9)
        assert classify_key(plan, 13) == ("session", 11)

    def test_other_no_dial_grid_uses_same_corner_math(self) -> None:
        # XL (4x8) has more than 3 columns, so it keeps the original corner
        # reservation -- only the exactly-3-columns Mini shape (below) gets
        # the bottom-row treatment.
        xl = plan_layout(CAPS_XL)
        assert (xl.view_key, xl.prev_key, xl.next_key) == (0, 24, 31)
        assert xl.sessions_per_page == 29


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
            assert classify_key(plan, key) == ("session", key)

    def test_dial_defaults(self) -> None:
        plan = plan_layout(CAPS_PLUS)
        assert plan.bindings["dial.0.turn"] == "view_cycle"
        assert plan.bindings["dial.0.push"] == "view_picker"
        assert plan.bindings["dial.1.turn"] == "page_cycle"
        assert plan.bindings["dial.1.push"] == "page_picker"
        # Dials 2/3 were "(unassigned)" pre-mapping -- now an explicit "none".
        assert plan.bindings["dial.2.turn"] == "none"
        assert plan.bindings["dial.2.push"] == "none"
        assert plan.bindings["dial.3.turn"] == "none"
        assert plan.bindings["dial.3.push"] == "none"


class TestCompactThreeColumnMode:
    """Stream Deck Mini shape (2 rows x 3 cols): bottom row is PREV/VIEW/NEXT.

    3 columns is exactly enough to dedicate a whole row to the 3 controls,
    so this grid shape gets bottom-row reservation instead of the corner
    scheme -- see `layout._reserved_control_keys`. Selected purely by
    `key_rows`/`key_cols`, never by `deck_type()`/model name.
    """

    def test_mode_and_reserved_indices(self) -> None:
        plan = plan_layout(CAPS_MINI)
        assert plan.mode == MODE_REDUCED
        assert plan.prev_key == 3  # bottom row, left
        assert plan.view_key == 4  # bottom row, middle
        assert plan.next_key == 5  # bottom row, right

    def test_session_slots_are_the_entire_top_row(self) -> None:
        plan = plan_layout(CAPS_MINI)
        assert plan.session_slots == (0, 1, 2)
        assert plan.sessions_per_page == 3

    def test_no_dials_no_strip(self) -> None:
        plan = plan_layout(CAPS_MINI)
        assert plan.use_dials is False
        assert plan.use_strip is False

    def test_classify_reserved_keys(self) -> None:
        plan = plan_layout(CAPS_MINI)
        assert classify_key(plan, 3) == ("page_prev", None)
        assert classify_key(plan, 4) == ("view_picker", None)
        assert classify_key(plan, 5) == ("page_next", None)

    def test_classify_session_keys_map_to_slot_positions(self) -> None:
        plan = plan_layout(CAPS_MINI)
        assert classify_key(plan, 0) == ("session", 0)
        assert classify_key(plan, 1) == ("session", 1)
        assert classify_key(plan, 2) == ("session", 2)

    def test_three_columns_with_extra_rows_still_reserves_bottom_row_only(
        self,
    ) -> None:
        # A hypothetical 3x3-cols deck (9 keys): still exactly 3 columns, so
        # the whole bottom row (not just its corners) is reserved -- the
        # two rows above it are entirely session tiles.
        caps = {
            "key_count": 9,
            "key_rows": 3,
            "key_cols": 3,
            "dial_count": 0,
            "is_touch": False,
        }
        plan = plan_layout(caps)
        assert (plan.prev_key, plan.view_key, plan.next_key) == (6, 7, 8)
        assert plan.session_slots == (0, 1, 2, 3, 4, 5)
        assert plan.sessions_per_page == 6

    def test_full_mode_wins_even_if_a_three_column_deck_reports_dials_and_touch(
        self,
    ) -> None:
        # Capability-driven priority: FULL mode's dial+touch check runs
        # before any REDUCED-mode geometry decision, so a (hypothetical)
        # 3-column deck with 2+ dials and a touchscreen is never routed
        # into the bottom-row special case.
        caps = {**CAPS_MINI, "dial_count": 2, "is_touch": True}
        plan = plan_layout(caps)
        assert plan.mode == MODE_FULL
        assert plan.view_key is None
        assert plan.prev_key is None
        assert plan.next_key is None
        assert plan.session_slots == tuple(range(6))
        assert plan.sessions_per_page == 6

    def test_dials_but_no_touch_still_uses_bottom_row_geometry(self) -> None:
        caps = {**CAPS_MINI, "dial_count": 4}
        plan = plan_layout(caps)
        assert plan.mode == MODE_REDUCED
        assert plan.use_dials is False
        assert plan.use_strip is False
        assert (plan.prev_key, plan.view_key, plan.next_key) == (3, 4, 5)

    def test_touch_but_one_dial_keeps_strip_with_bottom_row_geometry(self) -> None:
        caps = {**CAPS_MINI, "dial_count": 1, "is_touch": True}
        plan = plan_layout(caps)
        assert plan.mode == MODE_REDUCED
        assert plan.use_strip is True
        assert (plan.prev_key, plan.view_key, plan.next_key) == (3, 4, 5)


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


class TestZeroConfigEquivalence:
    """`plan_layout(caps, {})` must classify every control exactly as the

    pre-config-mapping implementation did. Expected tables are literal --
    pinned from v0.9.5, not re-derived from the new code -- so a
    regression here means a user who configures nothing sees a behavior
    change, which is the one thing this feature must never do.
    """

    def _assert_matches_table(self, caps: dict, expected: dict[str, str]) -> None:
        plan = plan_layout(caps, {})
        key_count = int(caps["key_count"])
        dial_count = int(caps["dial_count"])
        for key in range(key_count):
            address = f"key.{key}"
            assert plan.bindings[address] == expected[address], address
        for dial in range(dial_count):
            for sub in ("turn", "push"):
                address = f"dial.{dial}.{sub}"
                assert plan.bindings[address] == expected[address], address
        assert plan.unapplied == ()

    def test_original_15_matches_pinned_table(self) -> None:
        expected = {f"key.{k}": "session" for k in range(15)}
        for k in (1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13):
            expected[f"key.{k}"] = "session"
        expected["key.0"] = "view_picker"
        expected["key.10"] = "page_prev"
        expected["key.14"] = "page_next"
        self._assert_matches_table(CAPS_ORIGINAL_15, expected)

    def test_plus_matches_pinned_table(self) -> None:
        expected = {f"key.{k}": "session" for k in range(8)}
        expected.update(
            {
                "dial.0.turn": "view_cycle",
                "dial.0.push": "view_picker",
                "dial.1.turn": "page_cycle",
                "dial.1.push": "page_picker",
                "dial.2.turn": "none",
                "dial.2.push": "none",
                "dial.3.turn": "none",
                "dial.3.push": "none",
            }
        )
        self._assert_matches_table(CAPS_PLUS, expected)

    def test_xl_matches_pinned_table(self) -> None:
        expected = {f"key.{k}": "session" for k in range(32)}
        expected["key.0"] = "view_picker"
        expected["key.24"] = "page_prev"
        expected["key.31"] = "page_next"
        self._assert_matches_table(CAPS_XL, expected)

    def test_mini_matches_pinned_table(self) -> None:
        expected = {f"key.{k}": "session" for k in range(6)}
        expected["key.3"] = "page_prev"
        expected["key.4"] = "view_picker"
        expected["key.5"] = "page_next"
        self._assert_matches_table(CAPS_MINI, expected)

    def test_degenerate_grids_are_all_session(self) -> None:
        for caps in (
            {
                "key_count": 3,
                "key_rows": 1,
                "key_cols": 3,
                "dial_count": 0,
                "is_touch": False,
            },
            {
                "key_count": 6,
                "key_rows": 1,
                "key_cols": 6,
                "dial_count": 0,
                "is_touch": False,
            },
        ):
            expected = {f"key.{k}": "session" for k in range(caps["key_count"])}
            self._assert_matches_table(caps, expected)

    def test_default_bindings_matches_plan_layout_defaults(self) -> None:
        """`default_bindings` alone reproduces `plan_layout(caps, {}).bindings`."""
        for caps in (CAPS_ORIGINAL_15, CAPS_PLUS, CAPS_XL, CAPS_MINI):
            assert default_bindings(caps) == plan_layout(caps, {}).bindings

    def test_no_overrides_argument_matches_empty_overrides(self) -> None:
        """Calling `plan_layout(caps)` with no second arg matches `plan_layout(caps, {})`."""
        for caps in (CAPS_ORIGINAL_15, CAPS_PLUS, CAPS_XL, CAPS_MINI):
            assert plan_layout(caps).bindings == plan_layout(caps, {}).bindings


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
