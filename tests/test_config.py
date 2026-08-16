"""Config path expansion tests -- sudo-aware ``~`` resolution.

The sidecar is launched as ``sudo muxplex-deck`` for HID access; under sudo
a plain ``expanduser()`` resolves to ``/root``, not the invoking user's home.
These tests pin the path math of `_expand` / `_invoking_user_home` (using a
fake pwd database -- no real system users) and prove that without
``SUDO_USER`` the behavior is exactly the pre-existing ``expanduser()``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from muxplex_deck import config as config_mod
from muxplex_deck.config import ConfigError, _expand, load_config


class _FakePwd:
    """Stand-in for the pwd module: known users -> temp home dirs."""

    def __init__(self, known: dict[str, str]):
        self._known = known

    def getpwnam(self, name: str) -> SimpleNamespace:
        home = self._known.get(name)
        if home is None:
            raise KeyError(name)
        return SimpleNamespace(pw_dir=home)


@pytest.fixture
def sudo_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Simulate running under ``sudo deckuser`` with home at a temp dir."""
    home = tmp_path / "home" / "deckuser"
    home.mkdir(parents=True)
    monkeypatch.setenv("SUDO_USER", "deckuser")
    monkeypatch.setattr(config_mod, "pwd", _FakePwd({"deckuser": str(home)}))
    return home


def test_expand_under_sudo_uses_invoking_user_home(sudo_home: Path) -> None:
    result = _expand("~/.config/muxplex-deck/config.json")
    assert result == sudo_home / ".config/muxplex-deck/config.json"


def test_expand_bare_tilde_under_sudo(sudo_home: Path) -> None:
    assert _expand("~") == sudo_home


def test_expand_without_sudo_matches_expanduser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUDO_USER", raising=False)
    assert _expand("~/foo/bar") == Path("~/foo/bar").expanduser()


def test_expand_sudo_root_falls_back_to_normal_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUDO_USER", "root")
    assert _expand("~/foo") == Path("~/foo").expanduser()


def test_expand_unknown_sudo_user_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUDO_USER", "no-such-user")
    monkeypatch.setattr(config_mod, "pwd", _FakePwd({}))
    assert _expand("~/foo") == Path("~/foo").expanduser()


def test_expand_absolute_path_passes_through(sudo_home: Path) -> None:
    assert _expand("/etc/muxplex-deck.json") == Path("/etc/muxplex-deck.json")


def test_load_config_finds_defaults_under_sudo(
    sudo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: default config + key paths resolve under the sudo user."""
    monkeypatch.delenv("MUXPLEX_DECK_CONFIG", raising=False)
    conf_dir = sudo_home / ".config" / "muxplex-deck"
    conf_dir.mkdir(parents=True)
    (conf_dir / "config.json").write_text(
        '{"server_url": "https://example.test:8088"}', encoding="utf-8"
    )
    (conf_dir / "federation_key").write_text("sekrit\n", encoding="utf-8")

    cfg = load_config(None)

    assert cfg.server_url == "https://example.test:8088"
    assert cfg.federation_key == "sekrit"


def test_load_config_error_names_invoking_user_path(
    sudo_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-loud stays, and the message shows the CORRECT (sudo-user) path."""
    monkeypatch.delenv("MUXPLEX_DECK_CONFIG", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        load_config(None)
    assert str(sudo_home / ".config" / "muxplex-deck" / "config.json") in str(
        excinfo.value
    )
    assert "/root/" not in str(excinfo.value)


def test_missing_config_error_points_at_init_not_readme(tmp_path: Path) -> None:
    """The CLI teaches, docs supplement -- point at `muxplex-deck init`.

    v0.5.1 fixed `doctor`'s README pointer; this one survived in the
    sidecar's own runtime startup error (`load_config`'s "Config file not
    found" message) -- a real first-run user saw "See README.md for the
    full example." from the running process itself, not just from doctor.
    """
    missing = tmp_path / "config.json"
    with pytest.raises(ConfigError) as excinfo:
        load_config(str(missing))
    message = str(excinfo.value)
    assert "muxplex-deck init" in message
    assert "README" not in message


# ---------------------------------------------------------------------------
# `view_pin` -- Gate 1 parsing (deck-local view pinning, "Step 0")
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path: Path, extra: dict | None = None) -> Path:
    key_file = tmp_path / "federation_key"
    key_file.write_text("sekrit\n", encoding="utf-8")
    path = tmp_path / "config.json"
    data: dict = {
        "server_url": "https://example.test:8088",
        "key_file": str(key_file),
    }
    if extra:
        data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestViewPinParsing:
    def test_absent_view_pin_defaults_to_none(self, tmp_path: Path) -> None:
        """No `view_pin` key at all -- byte-identical to before the field existed."""
        path = _write_minimal_config(tmp_path)
        cfg = load_config(str(path))
        assert cfg.view_pin is None

    def test_explicit_null_view_pin_is_none(self, tmp_path: Path) -> None:
        path = _write_minimal_config(tmp_path, {"view_pin": None})
        cfg = load_config(str(path))
        assert cfg.view_pin is None

    def test_string_view_pin_is_kept(self, tmp_path: Path) -> None:
        path = _write_minimal_config(tmp_path, {"view_pin": "work"})
        cfg = load_config(str(path))
        assert cfg.view_pin == "work"

    def test_empty_string_view_pin_normalizes_to_none(self, tmp_path: Path) -> None:
        """An empty string means "not set", matching this file's `ca_file` convention."""
        path = _write_minimal_config(tmp_path, {"view_pin": ""})
        cfg = load_config(str(path))
        assert cfg.view_pin is None

    def test_non_string_view_pin_raises_config_error(self, tmp_path: Path) -> None:
        """Fails closed -- never silently coerces a bad type into a string."""
        path = _write_minimal_config(tmp_path, {"view_pin": 42})
        with pytest.raises(ConfigError) as excinfo:
            load_config(str(path))
        assert "view_pin" in str(excinfo.value)

    def test_non_string_view_pin_list_raises_config_error(self, tmp_path: Path) -> None:
        path = _write_minimal_config(tmp_path, {"view_pin": ["work"]})
        with pytest.raises(ConfigError):
            load_config(str(path))

    def test_view_pin_is_in_default_config_and_reloadable(self) -> None:
        """Full accounting: `view_pin` must be discoverable via `config list`/`config set`

        and hot-reloadable without a restart -- both silent-ignore failure
        modes this repo has hit before (the `focus_app` incident).
        """
        assert "view_pin" in config_mod.DEFAULT_CONFIG
        assert config_mod.DEFAULT_CONFIG["view_pin"] == ""  # sentinel, like ca_file
        assert "view_pin" in config_mod.RELOADABLE_KEYS
