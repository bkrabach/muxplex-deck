"""`muxplex-deck controls` (show/actions/set/unset/reset) + the `config set

controls` hard-refusal + `config get/list controls` special-casing
(docs/CONTROL_MAPPING_DESIGN.md §8). Pure file I/O against tmp config
paths; no hardware, no server -- device probing is monkeypatched to "no
deck" (matching the autouse safety rails) unless a test explicitly injects
a fake capability set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muxplex_deck import cli
from muxplex_deck.config import DEFAULT_CONFIG, load_raw_config, patch_raw_config


@pytest.fixture
def config_path(tmp_path: Path) -> str:
    return str(tmp_path / "config.json")


@pytest.fixture(autouse=True)
def _no_deck_and_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module runs with "no deck ever seen" by default.

    Individual tests override `cli._current_deck_caps` /
    `cli._last_seen_deck_caps` directly when they need a specific
    capability set (Gate 2 needs a deck to be meaningful).
    """
    monkeypatch.setattr(cli, "_current_deck_caps", lambda: None)
    monkeypatch.setattr(cli, "_last_seen_deck_caps", lambda: None)


CAPS_ORIGINAL_15 = {
    "key_count": 15,
    "key_rows": 3,
    "key_cols": 5,
    "dial_count": 0,
    "is_touch": False,
}


class TestConfigSetControlsRefusal:
    """`config set controls` must hard-refuse -- §8.1, and the exact live bug

    this design found: a `{}`-defaulted key falls through `config_set`'s
    isinstance chain and would silently store a raw string where a dict
    belongs.
    """

    def test_config_set_controls_exits_1_and_writes_nothing(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.config_set("controls", '{"key.0": "session"}', config_path)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "controls set" in err  # points at the real subcommand
        assert not Path(config_path).exists()

    def test_config_set_controls_points_at_dedicated_subcommand(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        with pytest.raises(SystemExit):
            cli.config_set("controls", "anything", config_path)
        assert "muxplex-deck controls set" in capsys.readouterr().err


class TestConfigGetListControls:
    def test_config_get_controls_prints_pretty_json(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        patch_raw_config({"controls": {"key.0": "session"}}, config_path)
        cli.config_get("controls", config_path)
        out = capsys.readouterr().out
        assert json.loads(out) == {"key.0": "session"}
        assert "\n" in out.strip()  # pretty-printed, not a one-line dump

    def test_config_get_controls_empty_prints_empty_object(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        cli.config_get("controls", config_path)
        assert json.loads(capsys.readouterr().out) == {}

    def test_config_list_shows_binding_count_not_raw_dict(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        patch_raw_config(
            {"controls": {"key.0": "session", "key.4": "view_picker"}}, config_path
        )
        cli.config_list(config_path)
        out = capsys.readouterr().out
        assert "2 bindings" in out
        assert "(modified)" in out
        assert "key.0" not in out  # not a raw dump

    def test_config_list_default_controls_shows_zero_bindings_no_modified_marker(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        cli.config_list(config_path)
        out = capsys.readouterr().out
        assert "controls: 0 bindings" in out
        for line in out.splitlines():
            if line.strip().startswith("controls:"):
                assert "(modified)" not in line


class TestControlsSetUnsetReset:
    def test_set_writes_one_binding(self, config_path: str) -> None:
        rc = cli.controls_set("key.0", "view_picker", config_path)
        assert rc == 0
        assert load_raw_config(config_path)["controls"] == {"key.0": "view_picker"}

    def test_set_invalid_action_rejected_exit_1_no_write(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        rc = cli.controls_set("key.0", "connect", config_path)
        assert rc == 1
        assert "connect" in capsys.readouterr().err
        assert load_raw_config(config_path)["controls"] == {}

    def test_set_kind_mismatch_rejected(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        rc = cli.controls_set("dial.0.turn", "view_picker", config_path)
        assert rc == 1
        assert "dial.0.turn" in capsys.readouterr().err

    def test_set_invalid_address_rejected(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        rc = cli.controls_set("key.1.press", "session", config_path)
        assert rc == 1
        assert load_raw_config(config_path)["controls"] == {}

    def test_unset_removes_one_binding(self, config_path: str) -> None:
        cli.controls_set("key.0", "view_picker", config_path)
        cli.controls_set("key.4", "session", config_path)
        rc = cli.controls_unset("key.0", config_path)
        assert rc == 0
        assert load_raw_config(config_path)["controls"] == {"key.4": "session"}

    def test_unset_absent_binding_is_a_no_op(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        rc = cli.controls_unset("key.7", config_path)
        assert rc == 0
        assert "already default" in capsys.readouterr().out
        assert load_raw_config(config_path)["controls"] == {}

    def test_reset_deletes_the_whole_controls_key(self, config_path: str) -> None:
        cli.controls_set("key.0", "view_picker", config_path)
        cli.controls_set("key.4", "session", config_path)
        rc = cli.controls_reset(config_path)
        assert rc == 0
        assert load_raw_config(config_path)["controls"] == {}

    def test_round_trip_set_unset_reset_leaves_file_byte_identical(
        self, config_path: str
    ) -> None:
        """`controls set` -> `unset` -> `reset` returns to the pre-`set` state

        (design test requirement #9).
        """
        before = load_raw_config(config_path)
        cli.controls_set("key.0", "view_picker", config_path)
        cli.controls_unset("key.0", config_path)
        after_unset = load_raw_config(config_path)
        assert after_unset == before

        cli.controls_set("key.4", "session", config_path)
        cli.controls_reset(config_path)
        after_reset = load_raw_config(config_path)
        assert after_reset == before


class TestControlsActions:
    def test_lists_all_19_actions(self, capsys: pytest.CaptureFixture) -> None:
        rc = cli.controls_actions()
        assert rc == 0
        out = capsys.readouterr().out
        for name in (
            "session",
            "view_picker",
            "view_cycle",
            "view_prev",
            "view_next",
            "page_first",
            "page_last",
            "focus_app",
            "refresh_now",
            "toggle_last",
            "brightness_up",
            "brightness_down",
            "brightness_cycle",
        ):
            assert name in out


class TestControlsShow:
    def test_no_deck_ever_seen_says_so_plainly(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        rc = cli.controls_show(config_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No deck currently connected" in out

    def test_invalid_config_reported_and_exits_1(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        Path(config_path).write_text(
            json.dumps({"controls": {"key.0": "bogus-action"}}), encoding="utf-8"
        )
        rc = cli.controls_show(config_path)
        assert rc == 1
        assert "bogus-action" in capsys.readouterr().err

    def test_resolved_table_against_connected_deck(
        self, config_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_current_deck_caps", lambda: CAPS_ORIGINAL_15)
        patch_raw_config({"controls": {"key.4": "view_picker"}}, config_path)
        rc = cli.controls_show(config_path)
        assert rc == 0

    def test_unapplied_binding_reported_not_fatal(
        self, config_path: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(cli, "_current_deck_caps", lambda: CAPS_ORIGINAL_15)
        patch_raw_config({"controls": {"dial.0.turn": "view_cycle"}}, config_path)
        rc = cli.controls_show(config_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "cannot apply" in out
        assert "dial.0.turn" in out
        assert "controls unset dial.0.turn" in out

    def test_last_seen_source_is_labeled_not_current(
        self, config_path: str, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        monkeypatch.setattr(cli, "_last_seen_deck_caps", lambda: CAPS_ORIGINAL_15)
        rc = cli.controls_show(config_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "last-seen" in out


class TestDoctorControlsCheck:
    def test_no_overrides_is_ok(self, config_path: str) -> None:
        status, message = cli.check_controls(config_path)
        assert status == "ok"
        assert "defaults" in message

    def test_invalid_config_is_a_failing_check(self, config_path: str) -> None:
        Path(config_path).write_text(
            json.dumps({"controls": {"key.0": "bogus"}}), encoding="utf-8"
        )
        status, _message = cli.check_controls(config_path)
        assert status == "fail"

    def test_valid_overrides_no_deck_is_ok_with_note(self, config_path: str) -> None:
        patch_raw_config({"controls": {"key.0": "session"}}, config_path)
        status, message = cli.check_controls(config_path)
        assert status == "ok"
        assert "no deck connected" in message

    def test_unapplied_binding_warns(
        self, config_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_current_deck_caps", lambda: CAPS_ORIGINAL_15)
        patch_raw_config({"controls": {"dial.0.turn": "view_cycle"}}, config_path)
        status, message = cli.check_controls(config_path)
        assert status == "warn"
        assert "dial.0.turn" in message

    def test_all_apply_is_ok(
        self, config_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli, "_current_deck_caps", lambda: CAPS_ORIGINAL_15)
        patch_raw_config({"controls": {"key.4": "view_picker"}}, config_path)
        status, message = cli.check_controls(config_path)
        assert status == "ok"
        assert "all apply" in message


class TestDefaultConfigKeyPresent:
    def test_controls_key_present_and_empty_by_default(self) -> None:
        assert DEFAULT_CONFIG["controls"] == {}
