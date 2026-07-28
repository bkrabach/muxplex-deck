"""Argument-parsing failures render through report.py's VERDICT/STATE/ACTION
bands instead of falling out to argparse's raw "usage: ...\\n<prog>: error:
..." text -- the gap real user feedback named directly: "what happened to
the click based help menu/feedback/errors? My feedback had been more on
the instructions being provided during failures."

Both real examples from that feedback are reproduced verbatim as
regression tests (`TestNearMissSuggestions`), plus the same mechanism
applied to `config`/`wsl` (`_ReportingArgumentParser` is inherited by
every subparser automatically -- see `cli.py`'s class docstring), the
no-close-match fallback, the bare-group-subcommand path
(`_render_missing_subcommand`, which replaces the old `print_help()`
fallback), and that exit codes are UNCHANGED throughout (2 for a genuine
parse error, 0 for a bare group command -- matching argparse's own
default `error()` and the pre-existing `print_help()` path respectively).
"""

from __future__ import annotations

import pytest

from muxplex_deck import cli


class TestNearMissSuggestions:
    """Both examples are the exact commands from the real user report."""

    def test_top_level_near_miss_suggests_corrected_full_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`muxplex-deck server status` -- the user meant `service status`."""
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "server", "status"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2

        err = capsys.readouterr().err
        assert "Not ready -- 1 thing to do." in err
        assert "invalid choice: 'server'" in err
        # Full valid-command list stays reachable in STATE...
        assert "'run'" in err and "'service'" in err
        # ...but the ACTION is the one corrected, runnable command --
        # note it retains "status", the argument that followed the typo.
        assert "muxplex-deck service status" in err

    def test_service_subcommand_near_miss_suggests_corrected_full_command(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """`muxplex-deck service log` -- the user meant `service logs`."""
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "service", "log"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2

        err = capsys.readouterr().err
        assert "invalid choice: 'log'" in err
        assert "muxplex-deck service logs" in err


class TestNearMissAppliesToEverySubparser:
    """`_ReportingArgumentParser` propagates via argparse's own
    `add_subparsers()` default (`parser_class=type(self)`) -- prove it
    reaches `config` and `wsl`, not just the root parser and `service`.
    """

    def test_config_subcommand_near_miss(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "config", "lsit"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice: 'lsit'" in err
        assert "muxplex-deck config list" in err

    def test_wsl_subcommand_near_miss(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "wsl", "atach"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "invalid choice: 'atach'" in err
        assert "muxplex-deck wsl attach" in err


class TestNoCloseMatchFallsBackToHelpProse:
    def test_top_level_gibberish_has_no_fabricated_suggestion(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "zzzzzzzzzzzzzzzz"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "Run 'muxplex-deck --help' to see valid commands." in err
        # No invented corrected-command line under "Do this:".
        assert "\n    muxplex-deck zzzzzzzzzzzzzzzz" not in err


class TestBareGroupSubcommandCoherent:
    """Bare `service`/`wsl` (no sub-subcommand) used to fall to argparse's
    raw `print_help()`; now renders through the same report.py bands as
    every other doctor/status/service surface. Exit code unchanged: like
    `print_help()`, this is not an error (no `sys.exit()` call on this path).
    """

    def test_bare_service_renders_report_lists_all_choices(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "service"])
        cli.main()  # no SystemExit expected on this path
        out = capsys.readouterr().out
        assert "Not ready -- 1 thing to do." in out
        assert "no subcommand given (choose from" in out
        # Full choice list stays reachable even though the STATE line wraps.
        collapsed = " ".join(out.split())
        assert "install, uninstall, start, stop, restart, status, logs)" in collapsed
        assert "muxplex-deck service install" in out

    def test_bare_wsl_renders_report(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "wsl"])
        cli.main()
        out = capsys.readouterr().out
        assert "no subcommand given (choose from attach)" in out
        assert "muxplex-deck wsl attach" in out


class TestExitCodesUnchanged:
    """Only the TEXT changed -- argparse's own exit-code contract (2 for a
    parse error) must hold exactly as before.
    """

    def test_invalid_choice_exit_code_is_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "bogus"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2

    def test_unrecognized_argument_still_routes_through_report_and_exits_two(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "version", "--nope"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "Not ready -- 1 thing to do." in err
        assert "unrecognized arguments" in err
