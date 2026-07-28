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

from muxplex_deck import report as report_mod
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
        monkeypatch.setattr(
            service_mod,
            "_enable_linger",
            lambda: report_mod.Check("linger", report_mod.FINE, "linger ok"),
        )
        monkeypatch.setattr(service_mod, "_udev_install_check", lambda: None)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

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

        def _fake_linger() -> Any:
            calls["linger"] = True
            return report_mod.Check("linger", report_mod.FINE, "linger ok")

        def _fake_udev() -> Any:
            calls["udev"] = True
            return None

        monkeypatch.setattr(service_mod, "_enable_linger", _fake_linger)
        monkeypatch.setattr(service_mod, "_udev_install_check", _fake_udev)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

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
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

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
# _udev_install_check() on WSL -- the rule is unproven there (AGENTS.md
# "U7"): a real WSL user followed the udev remediation `service install`
# printed and lost ~40 minutes, because the rule never actually fires for a
# usbip-attached device even when udev itself reports as "live". On WSL this
# must return the proven per-attach guidance instead of the raw udev block.
#
# Renamed from `_warn_if_no_udev_rule()` (which printed directly) as part of
# the report-based narration rewrite (v0.7.1) -- same branching, but now
# returns a `report.Check` (or None) for the install report to fold in,
# instead of printing mid-flight. These tests assert on the returned Check
# rather than captured stdout.
# ---------------------------------------------------------------------------


class TestUdevInstallCheckWslAware:
    def test_wsl_shows_environment_guidance_not_raw_udev_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from muxplex_deck import hidhelp, wsl

        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(
            wsl, "detect", lambda **_k: wsl.WslInfo(is_wsl=True, version=2, kernel="x")
        )
        monkeypatch.setattr(
            hidhelp,
            "explain_environment",
            lambda **_k: [
                hidhelp.Guidance(
                    status="warn", message="chown guidance line", state="W7"
                )
            ],
        )

        check = service_mod._udev_install_check()

        assert check is not None
        assert check.glyph == report_mod.ACT
        assert "chown guidance line" in check.value
        assert "70-streamdeck.rules" not in check.value

    def test_wsl_with_no_environment_guidance_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from muxplex_deck import hidhelp, wsl

        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(
            wsl, "detect", lambda **_k: wsl.WslInfo(is_wsl=True, version=2, kernel="x")
        )
        monkeypatch.setattr(hidhelp, "explain_environment", lambda **_k: [])

        assert service_mod._udev_install_check() is None

    def test_native_linux_container_udev_dead_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: non-WSL behavior must stay exactly as before --

        no Check returned when udev isn't live and this isn't WSL (the
        U-DEAD guidance -- if any -- still comes from `explain_environment()`
        calls elsewhere, e.g. `doctor()`, never duplicated here).
        """
        from muxplex_deck import usbnode, wsl

        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(
            wsl,
            "detect",
            lambda **_k: wsl.WslInfo(is_wsl=False, version=None, kernel="x"),
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: False)

        assert service_mod._udev_install_check() is None

    def test_native_linux_udev_live_shows_raw_udev_block_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: healthy native Linux still gets the udev block."""
        from muxplex_deck import usbnode, wsl

        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(
            wsl,
            "detect",
            lambda **_k: wsl.WslInfo(is_wsl=False, version=None, kernel="x"),
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: True)

        check = service_mod._udev_install_check()

        assert check is not None
        assert "70-streamdeck.rules" in check.value


# ---------------------------------------------------------------------------
# Unsupported platform
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# service_is_active
# ---------------------------------------------------------------------------


class TestServiceIsActive:
    def test_systemd_active_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0),
        )
        assert service_mod.service_is_active() is True

    def test_systemd_active_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(3),
        )
        assert service_mod.service_is_active() is False

    def test_launchd_active_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0),
        )
        assert service_mod.service_is_active() is True

    def test_missing_service_manager_is_false_not_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)

        def _raise(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("no systemctl")

        monkeypatch.setattr(service_mod.subprocess, "run", _raise)
        assert service_mod.service_is_active() is False


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# service_is_installed -- file-existence check, independent of active status.
# Fixes `doctor` conflating "not active" with "not installed" for an
# already-installed, crash-looping service (AGENTS.md incident).
# ---------------------------------------------------------------------------


class TestServiceIsInstalled:
    def test_systemd_installed_true_when_unit_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        unit_path = tmp_path / "muxplex-deck.service"
        unit_path.write_text("[Unit]\n")
        monkeypatch.setattr(service_mod, "_SYSTEMD_UNIT_PATH", unit_path)
        assert service_mod.service_is_installed() is True

    def test_systemd_installed_false_when_unit_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod, "_SYSTEMD_UNIT_PATH", tmp_path / "muxplex-deck.service"
        )
        assert service_mod.service_is_installed() is False

    def test_launchd_installed_true_when_plist_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        plist_path = tmp_path / "com.muxplex-deck.plist"
        plist_path.write_text("<plist></plist>\n")
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_PATH", plist_path)
        assert service_mod.service_is_installed() is True

    def test_launchd_installed_false_when_plist_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_PLIST_PATH", tmp_path / "com.muxplex-deck.plist"
        )
        assert service_mod.service_is_installed() is False


# ---------------------------------------------------------------------------
# _config_ready -- gates install/start on the SAME load_config() the running
# sidecar itself calls, so "ready to install" and "ready to run" can't diverge.
# ---------------------------------------------------------------------------


class TestConfigReady:
    def test_ready_when_config_loads(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config_path = tmp_path / "config.json"
        key_path = tmp_path / "federation_key"
        key_path.write_text("sekrit\n", encoding="utf-8")
        config_path.write_text(
            f'{{"server_url": "https://example.test:8088", "key_file": "{key_path}"}}',
            encoding="utf-8",
        )
        monkeypatch.setenv("MUXPLEX_DECK_CONFIG", str(config_path))
        ready, error = service_mod._config_ready()
        assert ready is True
        assert error is None

    def test_not_ready_when_config_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MUXPLEX_DECK_CONFIG", str(tmp_path / "no-such-config.json"))
        ready, error = service_mod._config_ready()
        assert ready is False
        assert error is not None
        assert "Config file not found" in error


# ---------------------------------------------------------------------------
# Crash-loop regression: `service install` must never enable/bootstrap a
# unit whose config is known-broken -- see AGENTS.md, "1113 restarts on a
# fresh machine with no config yet". Both systemd (`enable --now`) and
# launchd (`launchctl bootstrap`) must be skipped entirely when config isn't
# ready; the unit/plist file may still be written (harmless).
# ---------------------------------------------------------------------------


class TestSystemdInstallConfigGate:
    def _setup_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        monkeypatch.setattr(
            service_mod,
            "_enable_linger",
            lambda: report_mod.Check("linger", report_mod.FINE, "linger ok"),
        )
        monkeypatch.setattr(service_mod, "_udev_install_check", lambda: None)

    def test_missing_config_does_not_enable_or_start(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(
            service_mod, "_config_ready", lambda: (False, "Config file not found: X")
        )

        service_mod._systemd_install()

        # The unit file is still written (harmless), and daemon-reload still
        # runs, but `enable`/`enable --now` must NEVER be invoked -- that is
        # the exact crash-loop trigger.
        unit_path = tmp_path / "systemd" / "user" / "muxplex-deck.service"
        assert unit_path.exists()
        assert ["systemctl", "--user", "daemon-reload"] in recording_run.calls
        assert not any(
            call[:3] == ["systemctl", "--user", "enable"]
            for call in recording_run.calls
        )

        out = capsys.readouterr().out
        # Report-based narration (v0.7.1): the real invariant is that the
        # gate stops short of enabling, shows the real config error, and
        # directs the user to the fix -- not the old literal phrasing.
        assert "Not ready" in out
        assert "Config file not found: X" in out
        assert "muxplex-deck init" in out

    def test_ready_config_still_enables_and_starts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression guard: the healthy path is unchanged by the gate."""
        self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        service_mod._systemd_install()

        assert [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "muxplex-deck",
        ] in recording_run.calls
        out = capsys.readouterr().out
        assert "Enabled + started the service" in out
        assert "Not enabling or starting yet" not in out


class TestLaunchdInstallConfigGate:
    def _setup_paths(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    def test_missing_config_does_not_bootstrap(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(
            service_mod, "_config_ready", lambda: (False, "Config file not found: X")
        )

        service_mod._launchd_install()

        plist_path = tmp_path / "com.muxplex-deck.plist"
        assert plist_path.exists()
        assert not any(
            call[:2] == ["launchctl", "bootstrap"] for call in recording_run.calls
        )

        out = capsys.readouterr().out
        # Report-based narration (v0.7.1): the real invariant is that the
        # gate stops short of bootstrapping, shows the real config error,
        # and directs the user to the fix.
        assert "Not ready" in out
        assert "Config file not found: X" in out
        assert "muxplex-deck init" in out

    def test_ready_config_still_bootstraps(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression guard: the healthy path is unchanged by the gate."""
        self._setup_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        service_mod._launchd_install()

        bootstrap_calls = [
            c for c in recording_run.calls if c[:2] == ["launchctl", "bootstrap"]
        ]
        assert len(bootstrap_calls) == 1
        out = capsys.readouterr().out
        assert "Loaded + started the service" in out
        assert "Not enabling or starting yet" not in out


# ---------------------------------------------------------------------------
# Narration -- service install/uninstall must print step-by-step progress,
# not succeed silently (a real user reported `service install` gave no
# feedback at all).
# ---------------------------------------------------------------------------


class TestSystemdInstallNarration:
    def test_install_narrates_every_step(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
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
        monkeypatch.setattr(
            service_mod,
            "_enable_linger",
            lambda: report_mod.Check("linger", report_mod.FINE, "linger ok"),
        )
        monkeypatch.setattr(service_mod, "_udev_install_check", lambda: None)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

        service_mod._systemd_install()

        out = capsys.readouterr().out
        # Report-based narration (v0.7.1): one VERDICT/STATE/ACTION report
        # instead of a header + step-by-step prints + always-on "Next:"
        # banner -- the healthy case is quiet on success, matching
        # doctor()/status()'s own convention.
        assert "Wrote unit file" in out
        assert "Reloaded the systemd user daemon" in out
        assert "Enabled + started the service" in out
        assert "Service is running" in out

    def test_install_warns_when_not_reporting_active(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
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
        monkeypatch.setattr(
            service_mod,
            "_enable_linger",
            lambda: report_mod.Check("linger", report_mod.FINE, "linger ok"),
        )
        monkeypatch.setattr(service_mod, "_udev_install_check", lambda: None)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

        service_mod._systemd_install()

        out = capsys.readouterr().out
        assert "not reporting active" in out


class TestSystemdUninstallNarration:
    def test_uninstall_narrates_every_step(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        unit_path = tmp_path / "muxplex-deck.service"
        unit_path.write_text("[Unit]\n")
        monkeypatch.setattr(service_mod, "_SYSTEMD_UNIT_PATH", unit_path)

        service_mod._systemd_uninstall()

        out = capsys.readouterr().out
        assert "Stopped the service" in out
        assert "Disabled the service" in out
        assert "Removed unit file" in out
        assert "Reloaded the systemd user daemon" in out
        assert not unit_path.exists()

    def test_uninstall_reports_not_running_gracefully(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        unit_path = tmp_path / "muxplex-deck.service"
        monkeypatch.setattr(service_mod, "_SYSTEMD_UNIT_PATH", unit_path)

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:3] == ["systemctl", "--user", "stop"]:
                return _FakeCompletedProcess(1)
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._systemd_uninstall()

        out = capsys.readouterr().out
        assert "not running (nothing to stop)" in out
        assert "already absent" in out


class TestLaunchdInstallNarration:
    def test_install_narrates_every_step(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
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
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

        service_mod._launchd_install()

        out = capsys.readouterr().out
        assert "Wrote plist" in out
        assert "Loaded + started the service" in out
        assert "Service is running" in out


class TestLaunchdUninstallNarration:
    def test_uninstall_narrates_every_step(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        plist_path = tmp_path / "com.muxplex-deck.plist"
        plist_path.write_text("<plist></plist>\n")
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_PATH", plist_path)
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

        service_mod._launchd_uninstall()

        out = capsys.readouterr().out
        assert "Stopped + unloaded the service" in out
        assert "Removed plist" in out
        assert not plist_path.exists()


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


# ---------------------------------------------------------------------------
# service_manager_available -- gates whether callers (namely `init`) may
# even OFFER `service install`, not just whether dispatching to it would
# fail. See AGENTS.md's v0.5.2 crash-loop incident: never present an
# action that provably cannot succeed on this machine.
# ---------------------------------------------------------------------------


class TestServiceManagerAvailable:
    def test_true_on_darwin_regardless_of_systemctl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: False)
        assert service_mod.service_manager_available() is True

    def test_true_on_linux_with_systemctl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: True)
        assert service_mod.service_manager_available() is True

    def test_true_on_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Task Scheduler support (WINDOWS_NATIVE_SPEC.md section 1) makes

        this True on native Windows now -- it was False before this
        increment, when Windows had no supported service manager at all.
        """
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: False)
        assert service_mod.service_manager_available() is True

    def test_false_on_linux_without_systemctl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Minimal containers / distros without systemd -- same as WSL
        without systemd, another real case that must not be offered.
        """
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: False)
        assert service_mod.service_manager_available() is False


# ---------------------------------------------------------------------------
# launchd bootstrap idempotency -- a real user hit `service start` against an
# already-running service and got an unhandled CalledProcessError traceback
# instead of the benign no-op `launchctl bootstrap` exit 5 ("already loaded")
# actually is. These tests pin the exit-code branching with a mocked
# subprocess -- real launchctl is never invoked (see conftest.py Rail 4).
# ---------------------------------------------------------------------------


class TestLaunchdStart:
    def test_start_success_reports_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0),
        )
        service_mod._launchd_start()  # must not raise
        out = capsys.readouterr().out
        assert "Started the service" in out

    def test_start_already_loaded_exit5_is_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Pins the exact bug: bootstrap exit 5 is a benign no-op, not a failure."""
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                5, stderr="Bootstrap failed: 5: Input/output error"
            ),
        )
        service_mod._launchd_start()  # must NOT raise CalledProcessError
        captured = capsys.readouterr()
        assert "already running" in captured.out
        # Must not be reported as an error.
        assert captured.err == ""

    def test_start_genuine_failure_reports_diagnostics_and_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                1, stderr="Bootstrap failed: 1: Operation not permitted"
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            service_mod._launchd_start()
        assert exc_info.value.code == 1
        # Report-based narration (v0.7.1): the diagnostic now prints via
        # the report renderer on stdout, not a bare stderr "ERROR:" line --
        # the real invariant (the tool's own diagnostic stays visible, and
        # the process exits nonzero) is what's tested here.
        out = capsys.readouterr().out
        assert "bootstrap failed" in out
        assert "Operation not permitted" in out


class TestLaunchdInstallBootstrapIdempotency:
    def test_install_already_loaded_does_not_raise(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_DIR", tmp_path)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_PLIST_PATH", tmp_path / "com.muxplex-deck.plist"
        )
        monkeypatch.setattr(
            service_mod, "_resolve_bin_for_launchd", lambda: ["/x/muxplex-deck"]
        )
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["launchctl", "bootstrap"]:
                return _FakeCompletedProcess(
                    5, stderr="Bootstrap failed: 5: Input/output error"
                )
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._launchd_install()  # must not raise

        out = capsys.readouterr().out
        assert "already loaded" in out
        assert "service restart" in out

    def test_install_genuine_bootstrap_failure_reported_not_swallowed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_DIR", tmp_path)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_PLIST_PATH", tmp_path / "com.muxplex-deck.plist"
        )
        monkeypatch.setattr(
            service_mod, "_resolve_bin_for_launchd", lambda: ["/x/muxplex-deck"]
        )
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["launchctl", "bootstrap"]:
                return _FakeCompletedProcess(
                    1, stderr="Bootstrap failed: 1: Operation not permitted"
                )
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._launchd_install()  # must not raise -- no check=True crash

        # Report-based narration (v0.7.1): stdout, not a bare stderr "ERROR:"
        # line -- see the equivalent comment in TestLaunchdStart above.
        out = capsys.readouterr().out
        assert "bootstrap failed" in out
        assert "Operation not permitted" in out


class TestLaunchdRestart:
    def test_restart_waits_for_unload_before_bootstrapping(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Simulates the real bootout race: still active right after stop, then gone."""
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_BOOTOUT_POLL_INTERVAL_SECONDS", 0.001
        )

        active_sequence = iter([True, True, False])
        monkeypatch.setattr(
            service_mod, "service_is_active", lambda: next(active_sequence, False)
        )
        # This test is about the unload race, not the freshness wait added
        # separately (see TestRestartWaitsForFreshStatus) -- stub it out so
        # this test stays focused and fast.
        monkeypatch.setattr(
            service_mod, "_wait_for_fresh_status", lambda timeout=None: True
        )

        service_mod._launchd_restart()

        out = capsys.readouterr().out
        assert "Restarted the service" in out
        assert "did not fully unload" not in out

    def test_restart_timeout_warns_but_still_attempts_bootstrap(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The old job never reports gone -- restart must warn, then still try."""
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(service_mod, "_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(
            service_mod, "_LAUNCHD_BOOTOUT_POLL_INTERVAL_SECONDS", 0.001
        )
        # This test is about the unload-timeout warning, not the separate
        # freshness wait (see TestRestartWaitsForFreshStatus) -- stub it out.
        monkeypatch.setattr(
            service_mod, "_wait_for_fresh_status", lambda timeout=None: True
        )

        service_mod._launchd_restart()

        out = capsys.readouterr().out
        assert "did not fully unload" in out
        # Bootstrap is still attempted despite the timeout, and (per the
        # mocked exit-0 result) reports success.
        assert "Restarted the service" in out

    def test_restart_genuine_bootstrap_failure_after_clean_unload(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["launchctl", "bootstrap"]:
                return _FakeCompletedProcess(
                    1, stderr="Bootstrap failed: 1: Operation not permitted"
                )
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        with pytest.raises(SystemExit) as exc_info:
            service_mod._launchd_restart()
        assert exc_info.value.code == 1
        # Report-based narration (v0.7.1): stdout, not stderr.
        out = capsys.readouterr().out
        assert "bootstrap failed" in out


# ---------------------------------------------------------------------------
# systemd start/restart -- ordinary user action (start/restart before
# install, i.e. the unit doesn't exist yet) previously raised an unhandled
# CalledProcessError from check=True. Must now report cleanly and exit(1).
# ---------------------------------------------------------------------------


class TestSystemdStart:
    def test_start_success_reports_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        service_mod._systemd_start()  # must not raise
        out = capsys.readouterr().out
        assert "Started the service" in out

    def test_start_unit_not_found_reports_and_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                5, stderr="Failed to start muxplex-deck.service: Unit not found."
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            service_mod._systemd_start()
        assert exc_info.value.code == 1
        # Report-based narration (v0.7.1): stdout, not stderr.
        out = capsys.readouterr().out
        assert "Unit not found" in out


class TestSystemdRestart:
    def test_restart_success_reports_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        # This test is about the plain restart-success path -- the
        # freshness wait itself is covered by TestRestartWaitsForFreshStatus.
        monkeypatch.setattr(
            service_mod, "_wait_for_fresh_status", lambda timeout=None: True
        )
        service_mod._systemd_restart()  # must not raise
        out = capsys.readouterr().out
        assert "Restarted the service" in out

    def test_restart_unit_not_found_reports_and_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                5, stderr="Failed to restart muxplex-deck.service: Unit not found."
            ),
        )
        with pytest.raises(SystemExit) as exc_info:
            service_mod._systemd_restart()
        assert exc_info.value.code == 1
        # Report-based narration (v0.7.1): stdout, not stderr.
        out = capsys.readouterr().out
        assert "Unit not found" in out


# ---------------------------------------------------------------------------
# service_main_pid -- best-effort live pid lookup, never raises. Guards the
# bug CLASS this session found four times: state reported that wasn't
# actually observed this instant. This is the primitive that lets both
# `restart` and `status` tell "the process running right now" apart from
# "a previous incarnation's last write" -- see AGENTS.md's restart-race
# incident (2 false failures from a dying process's stale-but-recent write).
# ---------------------------------------------------------------------------


class TestServiceMainPid:
    def test_systemd_parses_pid_from_show_output(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, stdout="12345\n"),
        )
        assert service_mod.service_main_pid() == 12345

    def test_systemd_zero_pid_means_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MainPID is 0 (systemd's own convention) when the unit isn't active."""
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, stdout="0\n"),
        )
        assert service_mod.service_main_pid() is None

    def test_systemd_nonzero_exit_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(1, stderr="unit not found"),
        )
        assert service_mod.service_main_pid() is None

    def test_systemd_unparseable_output_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, stdout="not-a-number\n"),
        )
        assert service_mod.service_main_pid() is None

    def test_systemd_missing_binary_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)

        def _raise(*args: object, **kwargs: object) -> None:
            raise FileNotFoundError("no systemctl")

        monkeypatch.setattr(service_mod.subprocess, "run", _raise)
        assert service_mod.service_main_pid() is None

    def test_launchd_parses_pid_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                0,
                stdout=("com.muxplex-deck = {\n\tpid = 6789\n\tstate = running\n}\n"),
            ),
        )
        assert service_mod.service_main_pid() == 6789

    def test_launchd_not_loaded_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(3, stderr="Could not find service"),
        )
        assert service_mod.service_main_pid() is None


# ---------------------------------------------------------------------------
# _wait_for_fresh_status / restart integration -- the restart-race fix
# itself (AGENTS.md incident: `service restart` returned before the new
# process published status, so `status` read the dying old process's
# stale-but-recent snapshot and reported 2 false failures).
# ---------------------------------------------------------------------------


class TestRestartWaitsForFreshStatus:
    def test_waits_until_pid_matches_then_reports_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.001)

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:3] == ["systemctl", "--user", "restart"]:
                return _FakeCompletedProcess(0)
            if argv[:3] == ["systemctl", "--user", "show"]:
                return _FakeCompletedProcess(0, stdout="4321\n")
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        # First two polls still see the OLD process's snapshot (pid 1111);
        # the third sees the NEW process (pid 4321) has finally published.
        read_sequence = iter(
            [{"pid": 1111}, {"pid": 1111}, {"pid": 4321}, {"pid": 4321}]
        )
        monkeypatch.setattr(
            statusfile_mod,
            "read_status",
            lambda path=None: next(read_sequence, {"pid": 4321}),
        )

        service_mod._systemd_restart()

        out = capsys.readouterr().out
        assert "Restarted the service" in out
        assert "has not published fresh status" not in out

    def test_timeout_reports_honestly_never_a_false_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The new process's pid never shows up in status.json in time --

        must warn, never silently claim "Restarted the service" (see
        AGENTS.md: printing a check-mark for a step never verified is
        exactly the bug class this whole round of fixes closes).
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_RESTART_STATUS_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.005)

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:3] == ["systemctl", "--user", "restart"]:
                return _FakeCompletedProcess(0)
            if argv[:3] == ["systemctl", "--user", "show"]:
                return _FakeCompletedProcess(0, stdout="4321\n")
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)
        # status.json never catches up to the new pid within the timeout.
        monkeypatch.setattr(
            statusfile_mod, "read_status", lambda path=None: {"pid": 1111}
        )

        service_mod._systemd_restart()  # must not raise

        out = capsys.readouterr().out
        assert "has not published" in out
        assert "fresh" in out
        assert "Restarted the service" not in out

    def test_restart_failure_short_circuits_before_freshness_wait(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """A genuine restart failure must exit(1) without ever polling for

        freshness -- there is no new process to wait for.
        """
        from muxplex_deck import statusfile as statusfile_mod

        def _blow_up_if_called(path: object = None) -> None:
            raise AssertionError(
                "read_status should not be called when restart itself failed"
            )

        monkeypatch.setattr(statusfile_mod, "read_status", _blow_up_if_called)
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                5, stderr="Failed to restart muxplex-deck.service: Unit not found."
            ),
        )

        with pytest.raises(SystemExit) as exc_info:
            service_mod._systemd_restart()
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# `_reset_deck_best_effort()` -- the CLI-side fix for the deck staying lit
# after `service stop` hard-kills the sidecar (schtasks /End's
# TerminateProcess on Windows; a SIGKILL escalation on systemd/launchd).
# `main._shutdown_cleanup()` never runs on a hard kill, so THIS process
# opens the now-free device and resets it instead. Real HID is neutralized
# by `tests/conftest.py`'s autouse `_neutralize_real_hid` rail; these tests
# override `device_real.RealDeviceManager` explicitly per case, exactly the
# pattern `test_cli_doctor.py` already uses for `probe_deck_status`.
# ---------------------------------------------------------------------------


class _ResetFakeDeck:
    """Minimal fake `DeckDevice` for `_reset_deck_best_effort()` tests.

    `is_open()` reflects real open()/close() state (not just a flag some
    test flips) so `main._safe_close`'s own "never double-reset an
    already-closed device" guard is exercised honestly, not assumed.
    """

    def __init__(self, *, openable: bool = True) -> None:
        self._openable = openable
        self._open = False
        self.open_calls = 0
        self.reset_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        self.open_calls += 1
        if not self._openable:
            raise RuntimeError("Permission denied")
        self._open = True

    def is_open(self) -> bool:
        return self._open

    def reset(self) -> None:
        self.reset_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self._open = False


class _ResetFakeManager:
    def __init__(self, deck: _ResetFakeDeck | None) -> None:
        self._deck = deck

    def find_device(self) -> _ResetFakeDeck | None:
        return self._deck


class TestResetDeckBestEffort:
    def test_no_device_is_fine_not_an_action(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _ResetFakeManager(None)
        )
        check = service_mod._reset_deck_best_effort()
        assert check.glyph == report_mod.FINE
        assert "nothing to clear" in check.value

    def test_device_found_is_opened_reset_and_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        deck = _ResetFakeDeck(openable=True)
        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _ResetFakeManager(deck)
        )
        check = service_mod._reset_deck_best_effort()
        assert check.glyph == report_mod.FINE
        assert "Cleared the Stream Deck screen" in check.value
        assert deck.open_calls == 1
        assert deck.reset_calls == 1
        assert deck.close_calls == 1

    def test_open_failure_is_reported_but_never_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        deck = _ResetFakeDeck(openable=False)
        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _ResetFakeManager(deck)
        )
        check = service_mod._reset_deck_best_effort()  # must not raise
        assert check.glyph == report_mod.ACT
        assert "could not be opened" in check.value
        assert deck.reset_calls == 0

    def test_manager_construction_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        def _boom() -> Any:
            raise RuntimeError("hidapi missing")

        monkeypatch.setattr(device_real_mod, "RealDeviceManager", _boom)
        check = service_mod._reset_deck_best_effort()  # must not raise
        assert check.glyph == report_mod.ACT
        assert "could not access the Stream Deck" in check.value

    def test_enumeration_failure_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        class _BrokenManager:
            def find_device(self) -> None:
                raise RuntimeError("usb bus error")

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _BrokenManager()
        )
        check = service_mod._reset_deck_best_effort()  # must not raise
        assert check.glyph == report_mod.ACT
        assert "could not look for a Stream Deck" in check.value


# ---------------------------------------------------------------------------
# `_systemd_stop()` / `_launchd_stop()` -- reset only after the sidecar is
# CONFIRMED stopped, never before (never race a still-running process for
# the exclusive HID handle). Windows' equivalent gating lives in
# test_service_windows.py's TestWinStop.
# ---------------------------------------------------------------------------


class TestSystemdStopResetsDeck:
    def test_confirmed_stopped_attempts_reset(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        calls = {"n": 0}

        def _fake_reset() -> Any:
            calls["n"] += 1
            return report_mod.Check(
                "deck", report_mod.FINE, "Cleared the Stream Deck screen"
            )

        monkeypatch.setattr(service_mod, "_reset_deck_best_effort", _fake_reset)

        service_mod._systemd_stop()

        assert calls["n"] == 1
        out = capsys.readouterr().out
        assert "Cleared the Stream Deck screen" in out

    def test_still_active_skips_reset_never_races_the_device(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        def _blow_up_if_called() -> Any:
            raise AssertionError("must not attempt reset while still active")

        monkeypatch.setattr(service_mod, "_reset_deck_best_effort", _blow_up_if_called)

        service_mod._systemd_stop()  # must not raise

        out = capsys.readouterr().out
        assert "skipping screen clear" in out

    def test_device_error_during_reset_does_not_fail_stop(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)

        def _boom() -> Any:
            raise RuntimeError("device claimed elsewhere")

        monkeypatch.setattr(device_real_mod, "RealDeviceManager", _boom)

        service_mod._systemd_stop()  # must not raise, must not exit

        out = capsys.readouterr().out
        assert "could not access the Stream Deck" in out


class TestLaunchdStopResetsDeck:
    def test_confirmed_unloaded_attempts_reset(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(
            service_mod, "_wait_for_launchd_unload", lambda timeout=None: True
        )
        calls = {"n": 0}

        def _fake_reset() -> Any:
            calls["n"] += 1
            return report_mod.Check(
                "deck", report_mod.FINE, "Cleared the Stream Deck screen"
            )

        monkeypatch.setattr(service_mod, "_reset_deck_best_effort", _fake_reset)

        service_mod._launchd_stop()

        assert calls["n"] == 1
        out = capsys.readouterr().out
        assert "Cleared the Stream Deck screen" in out

    def test_unload_timeout_skips_reset_never_races_the_device(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(
            service_mod, "_wait_for_launchd_unload", lambda timeout=None: False
        )

        def _blow_up_if_called() -> Any:
            raise AssertionError("must not attempt reset before unload is confirmed")

        monkeypatch.setattr(service_mod, "_reset_deck_best_effort", _blow_up_if_called)

        service_mod._launchd_stop()  # must not raise

        out = capsys.readouterr().out
        assert "skipping screen clear" in out

    def test_device_error_during_reset_does_not_fail_stop(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(
            service_mod, "_wait_for_launchd_unload", lambda timeout=None: True
        )

        def _boom() -> Any:
            raise RuntimeError("device claimed elsewhere")

        monkeypatch.setattr(device_real_mod, "RealDeviceManager", _boom)

        service_mod._launchd_stop()  # must not raise, must not exit

        out = capsys.readouterr().out
        assert "could not access the Stream Deck" in out
