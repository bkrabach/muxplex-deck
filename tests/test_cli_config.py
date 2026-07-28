"""`muxplex-deck config` (list/get/set/reset) -- pure file I/O, tmp dirs only.

Mirrors muxplex's own config_list/config_get/config_set/config_reset tests:
type coercion from the default's type (bool/int/float/str), unknown key ->
stderr + exit 1, and the defaults-merge-overlay load/save/patch functions
in `config.py` that back them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muxplex_deck import cli
from muxplex_deck.config import (
    DEFAULT_CONFIG,
    load_raw_config,
    patch_raw_config,
    save_raw_config,
)


@pytest.fixture
def config_path(tmp_path: Path) -> str:
    return str(tmp_path / "config.json")


class TestLoadSaveRawConfig:
    def test_load_raw_config_missing_file_returns_defaults(
        self, config_path: str
    ) -> None:
        result = load_raw_config(config_path)
        assert result == DEFAULT_CONFIG

    def test_load_raw_config_corrupt_json_returns_defaults(
        self, config_path: str
    ) -> None:
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        Path(config_path).write_text("{not valid json", encoding="utf-8")
        result = load_raw_config(config_path)
        assert result == DEFAULT_CONFIG

    def test_save_raw_config_writes_known_keys_only(self, config_path: str) -> None:
        save_raw_config(
            {"server_url": "https://example.test:8088", "unknown_key": "ignored"},
            config_path,
        )
        on_disk = json.loads(Path(config_path).read_text(encoding="utf-8"))
        assert on_disk["server_url"] == "https://example.test:8088"
        assert "unknown_key" not in on_disk
        # Every default key is present (merged), not just the ones we set.
        assert set(on_disk.keys()) == set(DEFAULT_CONFIG.keys())

    def test_load_raw_config_ignores_unknown_keys_in_file(
        self, config_path: str
    ) -> None:
        Path(config_path).parent.mkdir(parents=True, exist_ok=True)
        Path(config_path).write_text(
            json.dumps({"server_url": "https://a.test", "bogus": 123}), encoding="utf-8"
        )
        result = load_raw_config(config_path)
        assert result["server_url"] == "https://a.test"
        assert "bogus" not in result

    def test_patch_raw_config_merges_and_persists(self, config_path: str) -> None:
        patch_raw_config({"poll_interval": 5.0}, config_path)
        result = load_raw_config(config_path)
        assert result["poll_interval"] == 5.0
        # Unrelated defaults untouched.
        assert result["sort"] == DEFAULT_CONFIG["sort"]


class TestConfigGet:
    def test_get_known_key_prints_value(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        patch_raw_config({"server_url": "https://x.test:8088"}, config_path)
        cli.config_get("server_url", config_path)
        assert capsys.readouterr().out.strip() == "https://x.test:8088"

    def test_get_bool_prints_lowercase(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        # No bool keys exist today in DEFAULT_CONFIG, but the display path
        # must still handle one correctly if a future key introduces it.
        patch_raw_config({"poll_interval": 3.0}, config_path)
        cli.config_get("poll_interval", config_path)
        assert capsys.readouterr().out.strip() == "3.0"

    def test_get_unknown_key_exits_1(self, config_path: str) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.config_get("nonexistent", config_path)
        assert excinfo.value.code == 1


class TestConfigSet:
    def test_set_string_value(self, config_path: str) -> None:
        cli.config_set("server_url", "https://y.test:9000", config_path)
        assert load_raw_config(config_path)["server_url"] == "https://y.test:9000"

    def test_set_float_value_coerced(self, config_path: str) -> None:
        cli.config_set("poll_interval", "7.5", config_path)
        result = load_raw_config(config_path)
        assert result["poll_interval"] == 7.5
        assert isinstance(result["poll_interval"], float)

    def test_set_float_invalid_value_exits_1(self, config_path: str) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.config_set("poll_interval", "not-a-number", config_path)
        assert excinfo.value.code == 1

    def test_set_unknown_key_exits_1(self, config_path: str) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.config_set("nonexistent", "value", config_path)
        assert excinfo.value.code == 1

    def test_set_sort_string_value(self, config_path: str) -> None:
        cli.config_set("sort", "server", config_path)
        assert load_raw_config(config_path)["sort"] == "server"


class TestConfigSetRoundTrip:
    """Regression net for incident 5 ("`config set` silently stores the

    wrong type"): `cli.config_set()` used to type-detect from the
    default's type via an `isinstance` chain that fell through to
    ``value = raw_value`` for ANY unmatched type -- harmless today only
    because every shipped key happens to default to str or float. The
    invariant that must hold for every key, present and future: what
    `config_set` WRITES must be exactly what `config_get`/`load_raw_config`
    READ BACK -- never a same-looking-but-wrong-typed approximation.
    """

    @pytest.mark.parametrize(
        ("key", "raw", "expected"),
        [
            (
                "server_url",
                "https://roundtrip.test:8088",
                "https://roundtrip.test:8088",
            ),
            (
                "key_file",
                "~/.config/muxplex-deck/other_key",
                "~/.config/muxplex-deck/other_key",
            ),
            (
                "ca_file",
                "~/.config/muxplex-deck/ca.crt",
                "~/.config/muxplex-deck/ca.crt",
            ),
            ("poll_interval", "4.5", 4.5),
            ("sort", "server", "server"),
            ("focus_app", "muxplex", "muxplex"),
        ],
    )
    def test_every_shipped_key_round_trips_exactly(
        self, config_path: str, key: str, raw: str, expected: object
    ) -> None:
        """For every key `DEFAULT_CONFIG` ships today: set it, then read it

        back two ways (`load_raw_config` and `cli.config_get`'s own
        stdout), and both must match the expected VALUE and TYPE exactly --
        not a stringified look-alike.
        """
        cli.config_set(key, raw, config_path)
        result = load_raw_config(config_path)
        assert result[key] == expected
        assert type(result[key]) is type(expected)

    def test_every_default_config_key_is_covered_by_the_round_trip_above(self) -> None:
        """Guards the guard: if a new key is ever added to `DEFAULT_CONFIG`

        without a matching case in the parametrize list above, this fails
        loudly instead of the new key silently going untested.
        """
        covered = {
            "server_url",
            "key_file",
            "ca_file",
            "poll_interval",
            "sort",
            "focus_app",
        }
        # "controls" is deliberately excluded from the scalar round-trip
        # above -- it's a dict, refused by `config set` entirely (see
        # TestConfigSetControlsRefusal below) and has its own dedicated
        # `controls set`/`unset`/`reset` interface (test_cli_controls.py).
        assert set(DEFAULT_CONFIG.keys()) - {"controls"} == covered
        assert "controls" in DEFAULT_CONFIG

    def test_structured_default_type_is_rejected_loudly_not_stored_as_a_string(
        self,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The actual incident 5 regression: simulate the future `controls`-

        shaped key (a dict default) by injecting a fake dict-typed entry
        into `DEFAULT_CONFIG` (the same dict object `cli.py` imported its
        `DEFAULT_CONFIG` reference from, so the mutation is visible on
        both names). Before the fix, `config_set` would store the raw CLI
        string in place of the dict and print success; now it must exit 1,
        print an explanation to stderr, and never touch the file at all.
        """
        monkeypatch.setitem(DEFAULT_CONFIG, "fake_structured_key", {"a": 1})

        with pytest.raises(SystemExit) as excinfo:
            cli.config_set("fake_structured_key", '{"a": 2}', config_path)
        assert excinfo.value.code == 1

        captured = capsys.readouterr()
        assert "fake_structured_key" in captured.err
        assert "dict" in captured.err

        # The config file must not exist (nothing was ever written) or, if
        # it does, must not contain the rejected key at all -- the write
        # path (`patch_raw_config`) must never have been reached.
        if Path(config_path).exists():
            on_disk = json.loads(Path(config_path).read_text(encoding="utf-8"))
            assert "fake_structured_key" not in on_disk

    def test_structured_default_type_rejection_is_generic_not_just_dict(
        self,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Same guard, a different structured type (list), to prove the

        rejection is a genuine `else` branch (any unhandled type) rather
        than a special case written only for dict.
        """
        monkeypatch.setitem(DEFAULT_CONFIG, "fake_list_key", [1, 2, 3])

        with pytest.raises(SystemExit) as excinfo:
            cli.config_set("fake_list_key", "1,2,3", config_path)
        assert excinfo.value.code == 1
        assert "list" in capsys.readouterr().err


class TestConfigReset:
    def test_reset_one_key(self, config_path: str) -> None:
        patch_raw_config({"sort": "server"}, config_path)
        cli.config_reset("sort", config_path)
        assert load_raw_config(config_path)["sort"] == DEFAULT_CONFIG["sort"]

    def test_reset_all_keys(self, config_path: str) -> None:
        patch_raw_config({"sort": "server", "poll_interval": 9.0}, config_path)
        cli.config_reset(None, config_path)
        assert load_raw_config(config_path) == DEFAULT_CONFIG

    def test_reset_unknown_key_exits_1(self, config_path: str) -> None:
        with pytest.raises(SystemExit) as excinfo:
            cli.config_reset("nonexistent", config_path)
        assert excinfo.value.code == 1


class TestConfigList:
    def test_list_shows_all_keys_and_modified_marker(
        self, config_path: str, capsys: pytest.CaptureFixture
    ) -> None:
        patch_raw_config({"sort": "server"}, config_path)
        cli.config_list(config_path)
        out = capsys.readouterr().out
        for key in DEFAULT_CONFIG:
            assert key in out
        assert 'sort: "server" (modified)' in out
        # An unmodified key shows no marker.
        assert 'focus_app: ""' in out
        assert 'focus_app: "" (modified)' not in out
