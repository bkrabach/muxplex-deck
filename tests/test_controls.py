"""Tests for the control-mapping catalog, address grammar, and both

validation gates (docs/CONTROL_MAPPING_DESIGN.md §3, §6).

- `TestAddressGrammar` / `TestCatalog`: pure `controls.py` unit tests.
- `TestKindCorrectness`: table-driven over all 19 actions -- the test that
  keeps the momentary/relative split honest as the catalog grows (design
  test requirement #10).
- `TestGate1Validation`: `config.load_config`'s capability-blind checks --
  one test per row of the design's §6 table, asserting `ConfigError` names
  both the offending value and the valid set.
- `TestGate2Applicability` / `TestMergeModel` / `TestAdvisories`: the
  capability-aware layer in `layout.plan_layout` -- reports, never
  refuses.
- `TestConfigSetControlsRefusal`: `config set controls` must hard-refuse
  (§8.1) -- covered again, end-to-end, in test_cli_controls.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muxplex_deck import controls
from muxplex_deck.config import ConfigError, load_config
from muxplex_deck.layout import Unapplied, plan_layout

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


class TestAddressGrammar:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("key.0", controls.Address("key", 0, None)),
            ("key.14", controls.Address("key", 14, None)),
            ("dial.0.turn", controls.Address("dial", 0, "turn")),
            ("dial.3.push", controls.Address("dial", 3, "push")),
        ],
    )
    def test_valid_addresses_parse(self, text: str, expected: controls.Address) -> None:
        assert controls.parse_address(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "key.00",  # leading zero
            "key.-1",  # sign
            "key.1.press",  # bogus sub
            "dial.0",  # missing sub
            "touch.tap",  # not in the grammar (deferred, §11.4)
            "KEY.0",  # case-sensitive
            "key.0 ",  # trailing space
            "",
            "key.",
            "dial.0.turnn",
        ],
    )
    def test_invalid_addresses_raise(self, text: str) -> None:
        with pytest.raises(controls.AddressError):
            controls.parse_address(text)

    def test_address_text_is_canonical(self) -> None:
        assert controls.Address("key", 5, None).text == "key.5"
        assert controls.Address("dial", 2, "push").text == "dial.2.push"

    def test_is_relative_only(self) -> None:
        assert controls.Address("dial", 0, "turn").is_relative_only is True
        assert controls.Address("dial", 0, "push").is_relative_only is False
        assert controls.Address("key", 0, None).is_relative_only is False


class TestCatalog:
    def test_catalog_has_19_actions(self) -> None:
        assert len(controls.ACTIONS) == 19

    def test_catalog_help_lines_cover_every_action(self) -> None:
        lines = controls.catalog_help_lines()
        assert len(lines) == len(controls.ACTIONS)
        for name in controls.ACTIONS:
            assert any(name in line for line in lines)


class TestKindCorrectness:
    """Every momentary action is legal on key.N/dial.N.push, illegal on

    dial.N.turn (except "none", universally valid -- see controls.py's
    module docstring). Every relative action is the mirror image. This is
    design test requirement #10: keeps the two-kind split honest as the
    catalog grows.
    """

    MOMENTARY_ACTIONS = tuple(
        sorted(
            name
            for name, spec in controls.ACTIONS.items()
            if spec.kind == controls.MOMENTARY and name != controls.NONE_ACTION
        )
    )
    RELATIVE_ACTIONS = tuple(
        sorted(
            name
            for name, spec in controls.ACTIONS.items()
            if spec.kind == controls.RELATIVE
        )
    )

    def test_all_19_actions_are_classified(self) -> None:
        assert len(self.MOMENTARY_ACTIONS) + len(self.RELATIVE_ACTIONS) + 1 == 19

    @pytest.mark.parametrize("action", MOMENTARY_ACTIONS)
    def test_momentary_action_accepted_on_key_and_dial_push(self, action: str) -> None:
        controls.validate_binding("key.0", action)
        controls.validate_binding("dial.0.push", action)

    @pytest.mark.parametrize("action", MOMENTARY_ACTIONS)
    def test_momentary_action_rejected_on_dial_turn(self, action: str) -> None:
        with pytest.raises(ValueError):
            controls.validate_binding("dial.0.turn", action)

    @pytest.mark.parametrize("action", RELATIVE_ACTIONS)
    def test_relative_action_accepted_on_dial_turn(self, action: str) -> None:
        controls.validate_binding("dial.0.turn", action)

    @pytest.mark.parametrize("action", RELATIVE_ACTIONS)
    def test_relative_action_rejected_on_key_and_dial_push(self, action: str) -> None:
        with pytest.raises(ValueError):
            controls.validate_binding("key.0", action)
        with pytest.raises(ValueError):
            controls.validate_binding("dial.0.push", action)

    def test_none_is_universally_valid(self) -> None:
        """Judgment call (controls.py docstring): "none" works on every address kind."""
        controls.validate_binding("key.0", "none")
        controls.validate_binding("dial.0.push", "none")
        controls.validate_binding("dial.0.turn", "none")


def _write_config(tmp_path: Path, extra: dict) -> str:
    path = tmp_path / "config.json"
    payload = {
        "server_url": "https://example.test:8088",
        "key_file": str(tmp_path / "federation_key"),
        **extra,
    }
    (tmp_path / "federation_key").write_text("fake-key\n", encoding="utf-8")
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


class TestGate1Validation:
    """`config.load_config`'s capability-blind checks (§6) -- fail closed,

    non-zero-exit-worthy `ConfigError`, one test per row of the table.
    """

    def test_empty_controls_loads_fine(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": {}})
        cfg = load_config(path)
        assert cfg.controls == {}

    def test_valid_controls_load_and_normalize(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": {"key.0": "session"}})
        cfg = load_config(path)
        assert cfg.controls == {"key.0": "session"}

    def test_controls_not_an_object_rejected(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": ["key.0", "session"]})
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "controls" in str(excinfo.value)
        assert "object" in str(excinfo.value)
        assert "list" in str(excinfo.value)

    def test_invalid_address_rejected(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": {"key.1.press": "session"}})
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "key.1.press" in str(excinfo.value)

    def test_non_string_value_rejected(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": {"key.0": 3}})
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        assert "key.0" in str(excinfo.value)
        assert "string" in str(excinfo.value)

    def test_unknown_action_rejected(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": {"key.0": "connect"}})
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        message = str(excinfo.value)
        assert "connect" in message
        assert "key.0" in message
        assert "session" in message  # part of the valid-actions list

    def test_kind_mismatch_rejected(self, tmp_path: Path) -> None:
        path = _write_config(tmp_path, {"controls": {"dial.0.turn": "view_picker"}})
        with pytest.raises(ConfigError) as excinfo:
            load_config(path)
        message = str(excinfo.value)
        assert "view_picker" in message
        assert "dial.0.turn" in message
        assert "view_cycle" in message  # part of the valid-for-this-address list

    def test_existing_config_with_no_controls_key_loads_unchanged(
        self, tmp_path: Path
    ) -> None:
        """A fresh install / pre-feature config has no "controls" key at all."""
        path = tmp_path / "config.json"
        (tmp_path / "federation_key").write_text("fake-key\n", encoding="utf-8")
        path.write_text(
            json.dumps(
                {
                    "server_url": "https://example.test:8088",
                    "key_file": str(tmp_path / "federation_key"),
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(str(path))
        assert cfg.controls == {}


class TestGate2Applicability:
    """`layout.plan_layout`'s capability-aware layer -- reports, never refuses."""

    def test_out_of_range_key_is_unapplied_not_fatal(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15, {"key.20": "session"})
        assert plan.unapplied == (
            Unapplied("key.20", "this deck has 15 keys (key.0 - key.14)"),
        )
        # The plan is still fully usable -- not refused.
        assert plan.session_slots

    def test_dial_binding_on_a_dialless_deck_is_unapplied(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15, {"dial.0.turn": "view_cycle"})
        assert plan.unapplied == (Unapplied("dial.0.turn", "this deck has no dials"),)

    def test_out_of_range_dial_is_unapplied(self) -> None:
        plan = plan_layout(CAPS_PLUS, {"dial.3.turn": "page_cycle"})
        assert plan.unapplied == ()  # dial.3 exists on the Deck+ (4 dials)
        plan2 = plan_layout(CAPS_PLUS, {"dial.7.turn": "page_cycle"})
        assert plan2.unapplied == (
            Unapplied("dial.7.turn", "this deck has 4 dials (dial.0 - dial.3)"),
        )

    def test_deck_swap_reused_config_reports_honestly(self) -> None:
        """A Deck+ config loaded against Original caps: reported, plan still usable."""
        plus_config = {
            "dial.2.turn": "page_cycle",
            "dial.2.push": "page_first",
            "dial.3.push": "view_all",
        }
        plan = plan_layout(CAPS_ORIGINAL_15, plus_config)
        assert len(plan.unapplied) == 3
        assert plan.session_slots  # still usable, not refused


class TestMergeModel:
    def test_one_key_override_recomputes_session_slots(self) -> None:
        plan = plan_layout(
            CAPS_ORIGINAL_15, {"key.0": "session", "key.4": "view_picker"}
        )
        assert plan.view_key == 4
        assert 0 in plan.session_slots
        assert 4 not in plan.session_slots
        # PREV/NEXT untouched -- still the original defaults.
        assert plan.prev_key == 10
        assert plan.next_key == 14
        assert plan.sessions_per_page == 12

    def test_none_excludes_key_from_session_slots(self) -> None:
        plan = plan_layout(CAPS_PLUS, {"key.3": "none"})
        assert 3 not in plan.session_slots
        assert plan.bindings["key.3"] == "none"

    def test_absent_key_keeps_default(self) -> None:
        plan = plan_layout(CAPS_PLUS, {"key.3": "none"})
        assert plan.bindings["key.5"] == "session"

    def test_reclaiming_dead_dials(self) -> None:
        plan = plan_layout(
            CAPS_PLUS,
            {
                "dial.2.turn": "page_cycle",
                "dial.2.push": "page_first",
                "dial.3.push": "view_all",
            },
        )
        assert plan.bindings["dial.2.turn"] == "page_cycle"
        assert plan.bindings["dial.2.push"] == "page_first"
        assert plan.bindings["dial.3.push"] == "view_all"
        # dial 0/1 keep their defaults -- untouched.
        assert plan.bindings["dial.0.turn"] == "view_cycle"
        assert plan.bindings["dial.1.turn"] == "page_cycle"
        assert plan.use_dials is True


class TestAdvisories:
    def test_no_session_binding_warns(self) -> None:
        overrides = {f"key.{k}": "none" for k in range(8)}
        plan = plan_layout(CAPS_PLUS, overrides)
        assert any(
            "no control is bound to 'session'" in note for note in plan.advisories
        )

    def test_view_picker_without_pager_warns(self) -> None:
        plan = plan_layout(
            CAPS_ORIGINAL_15,
            {"key.10": "session", "key.14": "session"},  # unbind PREV/NEXT
        )
        assert any("nothing pages it" in note for note in plan.advisories)

    def test_no_view_control_warns(self) -> None:
        plan = plan_layout(CAPS_ORIGINAL_15, {"key.0": "session"})  # unbind view_picker
        assert any("no control changes the view" in note for note in plan.advisories)

    def test_default_config_has_no_advisories(self) -> None:
        assert plan_layout(CAPS_ORIGINAL_15, {}).advisories == ()
        assert plan_layout(CAPS_PLUS, {}).advisories == ()
