"""Default-action dispatch: bare `muxplex-deck` == `muxplex-deck run`.

No real device/server is ever touched -- `cli.run` itself is monkeypatched
out so these tests only prove argparse wiring: which flags reach `run()`,
that they default to None (load-bearing for 3-tier config resolution), and
that the bare command and the explicit `run` subcommand produce identical
call signatures.
"""

from __future__ import annotations

import pytest

from muxplex_deck import cli


@pytest.fixture
def recorded_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict = {}

    def fake_run(
        config_path=None, *, emulator=False, emulator_port=8484, log_file=None
    ):
        calls["config_path"] = config_path
        calls["emulator"] = emulator
        calls["emulator_port"] = emulator_port
        calls["log_file"] = log_file
        return 0

    monkeypatch.setattr(cli, "run", fake_run)
    return calls


class TestDefaultActionDispatch:
    def test_bare_invocation_calls_run_with_defaults(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck"])
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 0
        assert recorded_run == {
            "config_path": None,
            "emulator": False,
            "emulator_port": 8484,
            "log_file": None,
        }

    def test_run_subcommand_identical_to_bare(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "run"])
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run == {
            "config_path": None,
            "emulator": False,
            "emulator_port": 8484,
            "log_file": None,
        }

    def test_bare_invocation_with_config_flag(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "--config", "/tmp/x.json"])
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["config_path"] == "/tmp/x.json"

    def test_run_subcommand_with_emulator_flag(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["muxplex-deck", "run", "--emulator", "--emulator-port", "9999"]
        )
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["emulator"] is True
        assert recorded_run["emulator_port"] == 9999

    def test_bare_invocation_with_emulator_flag_before_no_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "--emulator"])
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["emulator"] is True


class TestRunFlagsDefaultToNone:
    def test_root_parser_config_flag_defaults_to_none(self) -> None:
        parser_args = _parse(["muxplex-deck"])
        assert parser_args.config is None

    def test_run_subcommand_config_flag_defaults_to_none(self) -> None:
        parser_args = _parse(["muxplex-deck", "run"])
        assert parser_args.config is None

    def test_emulator_port_has_a_concrete_default(self) -> None:
        # Only --config participates in 3-tier resolution (CLI > file >
        # default) via None-sentinel; --emulator/--emulator-port are pure
        # CLI-only flags with no config.json counterpart, so a concrete
        # default is correct here (matches --emulator's pre-existing shape).
        parser_args = _parse(["muxplex-deck"])
        assert parser_args.emulator_port == 8484
        assert parser_args.emulator is False


def _parse(argv: list[str]):
    import argparse

    parser = argparse.ArgumentParser(prog="muxplex-deck")
    cli._add_run_flags(parser)
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run")
    cli._add_run_flags(run_parser)
    return parser.parse_args(argv[1:])


class TestOtherSubcommandsDoNotFallThroughToRun:
    def test_config_subcommand_does_not_call_run(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict, tmp_path
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["muxplex-deck", "--config", str(tmp_path / "c.json"), "config", "list"],
        )
        cli.main()
        assert recorded_run == {}

    def test_version_subcommand_does_not_call_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_run: dict,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "version"])
        cli.main()
        assert recorded_run == {}
        assert "muxplex-deck" in capsys.readouterr().out

    def test_bare_version_flag_does_not_call_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recorded_run: dict,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "--version"])
        cli.main()
        assert recorded_run == {}
        assert "muxplex-deck" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# --log-file: root-cause regression tests (WINDOWS_NATIVE_SPEC.md 1.5
# follow-up). Two independent bugs, both fixed here:
#
# 1. `cli.main()`'s final dispatch `else` branch never read `args.log_file`
#    back out and passed it to `run()` -- so no matter what argparse parsed,
#    the value was thrown away. This is what a real user hit: they ran the
#    EXACT Task Scheduler command by hand (`-m muxplex_deck run --log-file
#    <path>`) and no log file appeared, while `muxplex-deck run` (no
#    --log-file) logged to the console fine.
# 2. `--log-file` (and every other `_add_run_flags` flag) placed BEFORE the
#    `run` token was silently reset to the subparser's own default -- a
#    well-known argparse gotcha where the subparsers action parses tokens
#    after `run` into a fresh namespace and unconditionally overwrites the
#    parent namespace with it, including its own defaults for flags never
#    repeated after `run`. Fixed via `argparse.SUPPRESS` defaults on the
#    `run` subparser's copies (see `_add_run_flags`'s docstring).
# ---------------------------------------------------------------------------


class TestLogFileReachesRun:
    """`args.log_file` must reach `cli.run()` regardless of position."""

    def test_log_file_after_run_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["muxplex-deck", "run", "--log-file", "/tmp/after.log"]
        )
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["log_file"] == "/tmp/after.log"

    def test_log_file_before_run_subcommand(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        """The exact ordering the Windows Task Scheduler action originally

        produced when this bug was live -- see `service.py`'s
        `_win_task_arguments`.
        """
        monkeypatch.setattr(
            "sys.argv", ["muxplex-deck", "--log-file", "/tmp/before.log", "run"]
        )
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["log_file"] == "/tmp/before.log"

    def test_log_file_with_no_subcommand_at_all(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "--log-file", "/tmp/bare.log"])
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["log_file"] == "/tmp/bare.log"

    def test_no_log_file_still_defaults_to_none_in_both_positions(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr("sys.argv", ["muxplex-deck", "run"])
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["log_file"] is None


class TestSharedFlagOrderIndependence:
    """Every `_add_run_flags` flag -- not just --log-file -- must survive

    being placed before `run` (the argparse subparser-default clobber
    gotcha this class tests against is generic to shared dest names, not
    specific to any one flag).
    """

    def test_config_before_run_subcommand_is_not_clobbered(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["muxplex-deck", "--config", "/tmp/c.json", "run"]
        )
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["config_path"] == "/tmp/c.json"

    def test_emulator_before_run_subcommand_is_not_clobbered(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        monkeypatch.setattr(
            "sys.argv",
            ["muxplex-deck", "--emulator", "--emulator-port", "9999", "run"],
        )
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["emulator"] is True
        assert recorded_run["emulator_port"] == 9999

    def test_run_subparser_still_wins_when_flag_repeated_after_run(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict
    ) -> None:
        """Last-specified-wins: a flag given both before AND after `run`

        takes the value given after `run` (the subparser's own explicit
        parse, not a default) -- this is the case the SUPPRESS fix must
        NOT break.
        """
        monkeypatch.setattr(
            "sys.argv",
            [
                "muxplex-deck",
                "--log-file",
                "/tmp/before.log",
                "run",
                "--log-file",
                "/tmp/after.log",
            ],
        )
        with pytest.raises(SystemExit):
            cli.main()
        assert recorded_run["log_file"] == "/tmp/after.log"


class TestUnknownArgumentsStillErrorLoudly:
    """`cli.main()` uses `parser.parse_args()` (not a bare

    `parse_known_args()`), so unrecognized arguments are NOT silently
    discarded -- argparse's own leftover-argv check inside `parse_args()`
    still fires and routes through `_ReportingArgumentParser.error()` (see
    `test_cli_argument_errors.py`'s
    `test_unrecognized_argument_still_routes_through_report_and_exits_two`
    for the full regression test). `_ReportingArgumentParser` overrides
    `parse_known_args()` only to record argv for near-miss suggestions; it
    never overrides `parse_args()`, so that check is untouched. This test
    locks in the same guarantee specifically for the `run` subcommand,
    where the missing-log-file bug lived.
    """

    def test_unknown_flag_on_run_subcommand_errors_instead_of_silently_dropping(
        self, monkeypatch: pytest.MonkeyPatch, recorded_run: dict, capsys
    ) -> None:
        monkeypatch.setattr(
            "sys.argv", ["muxplex-deck", "run", "--totally-bogus-flag", "x"]
        )
        with pytest.raises(SystemExit) as excinfo:
            cli.main()
        assert excinfo.value.code == 2
        assert recorded_run == {}
        assert "unrecognized arguments" in capsys.readouterr().err
