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
