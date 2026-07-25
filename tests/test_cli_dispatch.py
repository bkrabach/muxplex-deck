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

    def fake_run(config_path=None, *, emulator=False, emulator_port=8484):
        calls["config_path"] = config_path
        calls["emulator"] = emulator
        calls["emulator_port"] = emulator_port
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
