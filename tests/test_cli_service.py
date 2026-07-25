"""`muxplex_deck.service` -- unit/plist generation and bin resolution.

Pure unit tests: `systemctl`/`launchctl`/`loginctl` are always mocked via
monkeypatching `subprocess.run` -- no real service manager is ever invoked,
no real service is installed on this host. Asserts the generated systemd
unit text and launchd plist XML directly (ExecStart / ProgramArguments,
PATH baking, Restart=always), and the two different bin-resolution shapes
(systemd wants a joined string, launchd wants a token LIST).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import service as service_mod


class _RecordingRun:
    """Stand-in for subprocess.run that records every invocation, does nothing real."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()


@pytest.fixture
def recording_run(monkeypatch: pytest.MonkeyPatch) -> _RecordingRun:
    recorder = _RecordingRun()
    monkeypatch.setattr(service_mod.subprocess, "run", recorder)
    return recorder


# ---------------------------------------------------------------------------
# Bin resolution
# ---------------------------------------------------------------------------


class TestBinResolution:
    def test_systemd_bin_which_hit_returns_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service_mod.shutil, "which", lambda name: "/usr/local/bin/muxplex-deck"
        )
        result = service_mod._resolve_muxplex_deck_bin()
        assert result == "/usr/local/bin/muxplex-deck"
        assert isinstance(result, str)

    def test_systemd_bin_which_miss_falls_back_to_python_dash_m(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod.shutil, "which", lambda name: None)
        result = service_mod._resolve_muxplex_deck_bin()
        assert result == f"{service_mod.sys.executable} -m muxplex_deck"

    def test_launchd_bin_prefers_local_bin_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        local_bin = tmp_path / ".local" / "bin" / "muxplex-deck"
        local_bin.parent.mkdir(parents=True)
        local_bin.write_text("#!/bin/sh\n")
        local_bin.chmod(0o755)
        monkeypatch.setattr(service_mod.Path, "home", lambda: tmp_path)
        result = service_mod._resolve_bin_for_launchd()
        assert result == [str(local_bin)]
        assert isinstance(result, list)

    def test_launchd_bin_falls_back_to_which(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(
            service_mod.shutil, "which", lambda name: "/opt/homebrew/bin/muxplex-deck"
        )
        result = service_mod._resolve_bin_for_launchd()
        assert result == ["/opt/homebrew/bin/muxplex-deck"]

    def test_launchd_bin_falls_back_to_python_dash_m_tokens(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(service_mod.shutil, "which", lambda name: None)
        result = service_mod._resolve_bin_for_launchd()
        assert result == [service_mod.sys.executable, "-m", "muxplex_deck"]
        # Explicitly split tokens, not a single shell string.
        assert len(result) == 3


# ---------------------------------------------------------------------------
# systemd unit generation
# ---------------------------------------------------------------------------


class TestSystemdInstall:
    def test_unit_content_has_correct_exec_start_and_restart_policy(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
    ) -> None:
        monkeypatch.setattr(
            service_mod, "_SYSTEMD_UNIT_DIR", tmp_path / "systemd" / "user"
        )
        monkeypatch.setattr(
            service_mod,
            "_SYSTEMD_UNIT_PATH",
            tmp_path / "systemd" / "user" / "muxplex-deck.service",
        )
        monkeypatch.setattr(
            service_mod.shutil, "which", lambda name: "/usr/bin/muxplex-deck"
        )
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setattr(service_mod, "_enable_linger", lambda: None)
        monkeypatch.setattr(service_mod, "_warn_if_no_udev_rule", lambda: None)

        service_mod._systemd_install()

        unit_path = tmp_path / "systemd" / "user" / "muxplex-deck.service"
        content = unit_path.read_text()
        assert "ExecStart=/usr/bin/muxplex-deck run" in content
        assert "Restart=always" in content
        assert "RestartSec=5s" in content
        assert "Environment=PATH=/usr/bin:/bin" in content
        assert "Description=muxplex-deck" in content

        # daemon-reload + enable --now were invoked (mocked, never real).
        assert ["systemctl", "--user", "daemon-reload"] in recording_run.calls
        assert [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "muxplex-deck",
        ] in recording_run.calls

    def test_install_calls_linger_and_udev_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
    ) -> None:
        monkeypatch.setattr(service_mod, "_SYSTEMD_UNIT_DIR", tmp_path)
        monkeypatch.setattr(
            service_mod, "_SYSTEMD_UNIT_PATH", tmp_path / "muxplex-deck.service"
        )
        monkeypatch.setattr(
            service_mod.shutil, "which", lambda name: "/usr/bin/muxplex-deck"
        )

        calls: dict[str, bool] = {"linger": False, "udev": False}
        monkeypatch.setattr(
            service_mod, "_enable_linger", lambda: calls.__setitem__("linger", True)
        )
        monkeypatch.setattr(
            service_mod,
            "_warn_if_no_udev_rule",
            lambda: calls.__setitem__("udev", True),
        )

        service_mod._systemd_install()

        assert calls == {"linger": True, "udev": True}


# ---------------------------------------------------------------------------
# launchd plist generation
# ---------------------------------------------------------------------------


class TestLaunchdInstall:
    def test_plist_program_arguments_is_array_form(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
    ) -> None:
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_DIR", tmp_path)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_PLIST_PATH", tmp_path / "com.muxplex-deck.plist"
        )
        monkeypatch.setattr(
            service_mod,
            "_resolve_bin_for_launchd",
            lambda: ["/opt/homebrew/bin/muxplex-deck"],
        )
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

        service_mod._launchd_install()

        plist_path = tmp_path / "com.muxplex-deck.plist"
        content = plist_path.read_text()
        assert "<key>Label</key>" in content
        assert "<string>com.muxplex-deck</string>" in content
        # ProgramArguments must be split into separate <string> elements --
        # one per argv token, NOT a single shell-joined string.
        assert "<string>/opt/homebrew/bin/muxplex-deck</string>" in content
        assert "<string>run</string>" in content
        assert "<string>/opt/homebrew/bin/muxplex-deck run</string>" not in content
        assert "<key>RunAtLoad</key>" in content
        assert "<true/>" in content
        assert "/tmp/muxplex-deck.log" in content
        assert "/tmp/muxplex-deck.err" in content

        bootstrap_calls = [
            c for c in recording_run.calls if c[:2] == ["launchctl", "bootstrap"]
        ]
        assert len(bootstrap_calls) == 1

    def test_plist_bakes_homebrew_paths_into_path_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
    ) -> None:
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_DIR", tmp_path)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_PLIST_PATH", tmp_path / "com.muxplex-deck.plist"
        )
        monkeypatch.setattr(
            service_mod, "_resolve_bin_for_launchd", lambda: ["/x/muxplex-deck"]
        )
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setenv("PATH", "/usr/bin:/bin")

        service_mod._launchd_install()

        content = (tmp_path / "com.muxplex-deck.plist").read_text()
        assert "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" in content

    def test_multi_token_bin_produces_multiple_string_elements(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
    ) -> None:
        """python -m muxplex_deck fallback: 3 tokens + 'run' = 4 <string> elements."""
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_DIR", tmp_path)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_PLIST_PATH", tmp_path / "com.muxplex-deck.plist"
        )
        monkeypatch.setattr(
            service_mod,
            "_resolve_bin_for_launchd",
            lambda: ["/usr/bin/python3", "-m", "muxplex_deck"],
        )
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

        service_mod._launchd_install()

        content = (tmp_path / "com.muxplex-deck.plist").read_text()
        # 4 ProgramArguments (3 bin tokens + "run") + Label + PATH + stdout + stderr = 8
        assert content.count("<string>") == 8
        assert "<string>/usr/bin/python3</string>" in content
        assert "<string>-m</string>" in content
        assert "<string>muxplex_deck</string>" in content
        assert "<string>run</string>" in content


# ---------------------------------------------------------------------------
# udev rule detection
# ---------------------------------------------------------------------------


class TestUdevRuleDetection:
    def test_no_rule_dirs_returns_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_UDEV_RULE_DIRS", (tmp_path / "nonexistent",))
        assert service_mod.udev_rule_exists() is False

    def test_matching_rule_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rules_dir = tmp_path / "rules.d"
        rules_dir.mkdir()
        (rules_dir / "70-streamdeck.rules").write_text(
            'SUBSYSTEM=="usb", ATTRS{idVendor}=="0fd9", MODE="0660"\n'
        )
        monkeypatch.setattr(service_mod, "_UDEV_RULE_DIRS", (rules_dir,))
        assert service_mod.udev_rule_exists() is True

    def test_unrelated_rule_not_matched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rules_dir = tmp_path / "rules.d"
        rules_dir.mkdir()
        (rules_dir / "50-other.rules").write_text(
            'SUBSYSTEM=="usb", ATTRS{idVendor}=="1234", MODE="0660"\n'
        )
        monkeypatch.setattr(service_mod, "_UDEV_RULE_DIRS", (rules_dir,))
        assert service_mod.udev_rule_exists() is False


# ---------------------------------------------------------------------------
# Unsupported platform
# ---------------------------------------------------------------------------


class TestUnsupportedPlatform:
    def test_service_install_reports_clear_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: False)
        service_mod.service_install()
        err = capsys.readouterr().err
        assert "requires systemd" in err
        assert "muxplex-deck run" in err
