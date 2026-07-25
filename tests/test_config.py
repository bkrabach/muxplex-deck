"""Config path expansion tests -- sudo-aware ``~`` resolution.

The sidecar is launched as ``sudo muxplex-deck`` for HID access; under sudo
a plain ``expanduser()`` resolves to ``/root``, not the invoking user's home.
These tests pin the path math of `_expand` / `_invoking_user_home` (using a
fake pwd database -- no real system users) and prove that without
``SUDO_USER`` the behavior is exactly the pre-existing ``expanduser()``.
"""

from __future__ import annotations

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
