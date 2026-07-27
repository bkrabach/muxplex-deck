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
# _warn_if_no_udev_rule() on WSL -- the rule is unproven there (AGENTS.md
# "U7"): a real WSL user followed the udev remediation `service install`
# printed and lost ~40 minutes, because the rule never actually fires for a
# usbip-attached device even when udev itself reports as "live". On WSL this
# must show the proven per-attach guidance instead of the raw udev block.
# ---------------------------------------------------------------------------


class TestWarnIfNoUdevRuleWslAware:
    def test_wsl_shows_environment_guidance_not_raw_udev_block(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
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

        service_mod._warn_if_no_udev_rule()

        out = capsys.readouterr().out
        assert "chown guidance line" in out
        assert "70-streamdeck.rules" not in out
        assert "<<'EOF'" not in out

    def test_wsl_with_no_environment_guidance_prints_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from muxplex_deck import hidhelp, wsl

        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(
            wsl, "detect", lambda **_k: wsl.WslInfo(is_wsl=True, version=2, kernel="x")
        )
        monkeypatch.setattr(hidhelp, "explain_environment", lambda **_k: [])

        service_mod._warn_if_no_udev_rule()

        out = capsys.readouterr().out
        assert out == ""

    def test_native_linux_container_udev_dead_is_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression guard: non-WSL behavior must stay exactly as before --

        no guidance printed by `_warn_if_no_udev_rule()` itself when udev
        isn't live and this isn't WSL (the U-DEAD guidance -- if any --
        still comes from `explain_environment()` calls elsewhere, e.g.
        `doctor()`, never duplicated here).
        """
        from muxplex_deck import usbnode, wsl

        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(
            wsl,
            "detect",
            lambda **_k: wsl.WslInfo(is_wsl=False, version=None, kernel="x"),
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: False)

        service_mod._warn_if_no_udev_rule()

        out = capsys.readouterr().out
        assert out == ""

    def test_native_linux_udev_live_shows_raw_udev_block_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
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

        service_mod._warn_if_no_udev_rule()

        out = capsys.readouterr().out
        assert "70-streamdeck.rules" in out


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
        monkeypatch.setattr(service_mod, "_enable_linger", lambda: None)
        monkeypatch.setattr(service_mod, "_warn_if_no_udev_rule", lambda: None)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        service_mod._systemd_install()

        out = capsys.readouterr().out
        assert "service install (systemd" in out
        assert "Wrote unit file" in out
        assert "Reloaded the systemd user daemon" in out
        assert "Enabled + started the service" in out
        assert "Service is running" in out
        assert "muxplex-deck status" in out
        assert "muxplex-deck service logs" in out

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
        monkeypatch.setattr(service_mod, "_enable_linger", lambda: None)
        monkeypatch.setattr(service_mod, "_warn_if_no_udev_rule", lambda: None)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)

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
        assert "service uninstall (systemd" in out
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

        service_mod._launchd_install()

        out = capsys.readouterr().out
        assert "service install (launchd)" in out
        assert "Wrote plist" in out
        assert "Loaded + started the service" in out
        assert "Service is running" in out
        assert "muxplex-deck status" in out
        assert "muxplex-deck service logs" in out


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
        assert "service uninstall (launchd)" in out
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
        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "Operation not permitted" in err


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

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["launchctl", "bootstrap"]:
                return _FakeCompletedProcess(
                    1, stderr="Bootstrap failed: 1: Operation not permitted"
                )
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._launchd_install()  # must not raise -- no check=True crash

        err = capsys.readouterr().err
        assert "ERROR: launchctl bootstrap failed" in err
        assert "Operation not permitted" in err


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
        err = capsys.readouterr().err
        assert "ERROR: launchctl bootstrap failed" in err


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
        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "Unit not found" in err


class TestSystemdRestart:
    def test_restart_success_reports_ok(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
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
        err = capsys.readouterr().err
        assert "ERROR" in err
        assert "Unit not found" in err
