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

from muxplex_deck import cli, statusfile
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


class TestControlsSetReloadReporting:
    """`controls set`/`unset`/`reset` must say whether a running sidecar

    picked the edit up -- "config written but not applied" is the exact
    stale-state class this repo shipped five times in one day (see
    AGENTS.md). No real sidecar process here -- `status.json` is written
    directly, exactly as the real one would.
    """

    def test_no_running_sidecar_says_so_plainly(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        """No status.json at all -- the honest "not running" case, not a hang."""
        rc = cli.controls_set("key.0", "view_picker", config_path)
        assert rc == 0
        out = capsys.readouterr().out
        assert "no running sidecar detected" in out

    def test_confirmed_pickup_reports_applied(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        """A status file already showing THIS write's mtime (or newer) is

        reported as applied immediately -- no actual waiting needed for
        the common case where the sidecar is faster than the CLI. Calls
        `_report_reload_effect` directly (rather than through a second
        `controls_set`) so the write whose mtime we seed the status with
        is the same write being reported on -- a second real write would
        advance the file's mtime again and invalidate the seeded value.
        """
        cli.controls_set("key.0", "view_picker", config_path)
        capsys.readouterr()
        written_mtime = Path(config_path).stat().st_mtime

        statusfile.write_status(
            statusfile.build_status(
                pid=1234,
                device_connected=True,
                device_caps=None,
                server_url="https://example.test:8088",
                server_connected=True,
                last_poll_at=None,
                last_error=None,
                active_session=None,
                active_view=None,
                page=None,
                config_reload={
                    "config_mtime": written_mtime,
                    "checked_at": written_mtime,
                    "applied": ["controls"],
                    "restart_required": [],
                    "error": None,
                },
            )
        )

        cli._report_reload_effect(config_path, {})
        out = capsys.readouterr().out
        assert "applied to the running sidecar" in out

    def test_sidecar_rejection_is_surfaced_as_an_error(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        """If the running sidecar's last reload attempt failed (e.g. some

        OTHER concurrent hand-edit broke Gate 1), that error is surfaced
        to the user instead of a false "applied".
        """
        cli.controls_set("key.0", "view_picker", config_path)
        capsys.readouterr()
        written_mtime = Path(config_path).stat().st_mtime

        statusfile.write_status(
            statusfile.build_status(
                pid=1234,
                device_connected=True,
                device_caps=None,
                server_url="https://example.test:8088",
                server_connected=True,
                last_poll_at=None,
                last_error=None,
                active_session=None,
                active_view=None,
                page=None,
                config_reload={
                    "config_mtime": written_mtime,
                    "checked_at": written_mtime,
                    "applied": [],
                    "restart_required": [],
                    "error": "Config field 'controls' has unknown action 'bogus'",
                },
            )
        )

        cli._report_reload_effect(config_path, {})
        captured = capsys.readouterr()
        assert "sidecar rejected the config" in captured.err

    def test_stale_status_never_hangs_reports_unconfirmed(
        self,
        config_path: str,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A status file exists (so a sidecar has run at some point) but its

        `config_reload.config_mtime` never catches up with our write --
        bounded wait, never an infinite hang, and never falsely claims
        "applied".
        """
        statusfile.write_status(
            statusfile.build_status(
                pid=1234,
                device_connected=True,
                device_caps=None,
                server_url="https://example.test:8088",
                server_connected=True,
                last_poll_at=None,
                last_error=None,
                active_session=None,
                active_view=None,
                page=None,
                config_reload={
                    "config_mtime": 1.0,  # far in the past, never advances
                    "checked_at": 1.0,
                    "applied": [],
                    "restart_required": [],
                    "error": None,
                },
            )
        )
        monkeypatch.setattr(cli, "_RELOAD_CONFIRM_POLL_SECONDS", 0.01)
        monkeypatch.setattr(cli, "_RELOAD_CONFIRM_MIN_TIMEOUT_SECONDS", 0.05)
        monkeypatch.setattr(cli, "_RELOAD_CONFIRM_MAX_TIMEOUT_SECONDS", 0.05)

        rc = cli.controls_set("key.0", "view_picker", config_path)

        assert rc == 0
        out = capsys.readouterr().out
        assert "hasn't confirmed picking this up yet" in out

    def test_unset_and_reset_also_report(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        cli.controls_set("key.0", "view_picker", config_path)
        capsys.readouterr()  # drain

        cli.controls_unset("key.0", config_path)
        assert "no running sidecar detected" in capsys.readouterr().out

        cli.controls_set("key.0", "view_picker", config_path)
        capsys.readouterr()
        cli.controls_reset(config_path)
        assert "no running sidecar detected" in capsys.readouterr().out


class TestReloadConfirmTimeout:
    def test_derives_from_poll_interval_bounded(self) -> None:
        assert cli._reload_confirm_timeout({"poll_interval": 2.0}) == pytest.approx(3.0)
        # A huge poll_interval is still capped.
        assert (
            cli._reload_confirm_timeout({"poll_interval": 100.0})
            == cli._RELOAD_CONFIRM_MAX_TIMEOUT_SECONDS
        )
        # A missing/invalid poll_interval falls back to the config default.
        assert cli._reload_confirm_timeout({}) == pytest.approx(3.0)
        assert cli._reload_confirm_timeout({"poll_interval": -1}) == pytest.approx(3.0)


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
