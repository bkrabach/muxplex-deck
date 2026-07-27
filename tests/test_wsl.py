"""`muxplex_deck.wsl` -- detection, usbipd parsing, and the one mutating call.

`subprocess.run` is mocked throughout via monkeypatch -- the autouse safety
rail in `conftest.py` blocks the real thing anyway (see
`test_safety_rails.py`), so any test here that "forgets" to mock it fails
loudly rather than shelling out to a real `usbipd.exe`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import wsl

# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------


class TestDetect:
    def test_wsl2_kernel_string(self, tmp_path: Path) -> None:
        osrelease = tmp_path / "osrelease"
        osrelease.write_text("5.15.90.1-microsoft-standard-WSL2\n", encoding="utf-8")
        info = wsl.detect(osrelease_path=osrelease)
        assert info.is_wsl is True
        assert info.version == 2

    def test_wsl1_kernel_string(self, tmp_path: Path) -> None:
        osrelease = tmp_path / "osrelease"
        osrelease.write_text("4.4.0-19041-Microsoft\n", encoding="utf-8")
        info = wsl.detect(osrelease_path=osrelease)
        assert info.is_wsl is True
        assert info.version == 1

    def test_native_linux_kernel_string(self, tmp_path: Path) -> None:
        osrelease = tmp_path / "osrelease"
        osrelease.write_text("6.17.0-1014-nvidia\n", encoding="utf-8")
        info = wsl.detect(osrelease_path=osrelease)
        assert info.is_wsl is False
        assert info.version is None

    def test_missing_osrelease_is_not_wsl_not_raising(self, tmp_path: Path) -> None:
        info = wsl.detect(osrelease_path=tmp_path / "nonexistent")
        assert info.is_wsl is False
        assert info.version is None


# ---------------------------------------------------------------------------
# find_usbipd()
# ---------------------------------------------------------------------------


class TestFindUsbipd:
    def test_only_windows_binary_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _which(name: str) -> str | None:
            return (
                "/mnt/c/Program Files/usbipd-win/usbipd.exe"
                if name == "usbipd.exe"
                else None
            )

        monkeypatch.setattr(wsl.shutil, "which", _which)
        paths = wsl.find_usbipd()
        assert paths.windows is not None
        assert paths.linux_impostor is None

    def test_neither_binary_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl.shutil, "which", lambda name: None)
        paths = wsl.find_usbipd()
        assert paths.windows is None
        assert paths.linux_impostor is None

    def test_impostor_detected_when_bare_name_differs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _which(name: str) -> str | None:
            if name == "usbipd.exe":
                return "/mnt/c/Program Files/usbipd-win/usbipd.exe"
            if name == "usbipd":
                return "/usr/bin/usbipd"
            return None

        monkeypatch.setattr(wsl.shutil, "which", _which)
        paths = wsl.find_usbipd()
        assert paths.windows is not None
        assert paths.linux_impostor == Path("/usr/bin/usbipd")

    def test_not_an_impostor_when_bare_name_resolves_to_same_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _which(name: str) -> str | None:
            return "/mnt/c/usbipd.exe"

        monkeypatch.setattr(wsl.shutil, "which", _which)
        paths = wsl.find_usbipd()
        assert paths.linux_impostor is None


# ---------------------------------------------------------------------------
# list_devices() -- parsing `usbipd.exe list` output
# ---------------------------------------------------------------------------

_LIST_OUTPUT_SHARED = """\
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-4    0fd9:006d  Elgato Stream Deck                                            Shared

Persisted:
GUID                                  DEVICE
"""

_LIST_OUTPUT_NOT_SHARED = """\
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-4    0fd9:006d  Elgato Stream Deck                                            Not shared

Persisted:
"""

_LIST_OUTPUT_ATTACHED = """\
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-4    0fd9:006d  Elgato Stream Deck                                            Attached

Persisted:
"""

_LIST_OUTPUT_UNKNOWN_STATE = """\
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
1-4    0fd9:006d  Elgato Stream Deck                                            SomeNewState

Persisted:
"""

_LIST_OUTPUT_EMPTY = """\
Connected:
BUSID  VID:PID    DEVICE                                                        STATE

Persisted:
"""

_LIST_OUTPUT_OTHER_DEVICE_ONLY = """\
Connected:
BUSID  VID:PID    DEVICE                                                        STATE
2-1    046d:c52b  Logitech USB Receiver                                         Not shared

Persisted:
"""


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestListDevices:
    def test_shared_state_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(stdout=_LIST_OUTPUT_SHARED),
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices is not None
        assert len(devices) == 1
        assert devices[0].busid == "1-4"
        assert devices[0].vid_pid == "0fd9:006d"
        assert devices[0].state == "shared"

    def test_not_shared_state_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(stdout=_LIST_OUTPUT_NOT_SHARED),
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices is not None
        assert devices[0].state == "not_shared"

    def test_attached_state_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(stdout=_LIST_OUTPUT_ATTACHED),
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices is not None
        assert devices[0].state == "attached"

    def test_unknown_state_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(stdout=_LIST_OUTPUT_UNKNOWN_STATE),
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices is not None
        assert devices[0].state == "unknown"

    def test_persisted_section_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        output = _LIST_OUTPUT_SHARED.replace(
            "Persisted:\nGUID                                  DEVICE\n",
            "Persisted:\nGUID                                  DEVICE\n"
            "abcd1234-0000-0000-0000-000000000000  0fd9:006d  Old Stream Deck\n",
        )
        monkeypatch.setattr(
            wsl.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(stdout=output)
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices is not None
        assert len(devices) == 1  # the persisted-section duplicate is NOT counted

    def test_empty_connected_section_returns_empty_list_not_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(stdout=_LIST_OUTPUT_EMPTY),
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices == []

    def test_other_vendor_device_filtered_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                stdout=_LIST_OUTPUT_OTHER_DEVICE_ONLY
            ),
        )
        devices = wsl.list_devices(Path("/fake/usbipd.exe"))
        assert devices == []

    def test_timeout_returns_none_not_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a: Any, **k: Any) -> Any:
            raise subprocess.TimeoutExpired(cmd="usbipd.exe list", timeout=5.0)

        monkeypatch.setattr(wsl.subprocess, "run", _raise)
        assert wsl.list_devices(Path("/fake/usbipd.exe")) is None

    def test_file_not_found_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("no such file")

        monkeypatch.setattr(wsl.subprocess, "run", _raise)
        assert wsl.list_devices(Path("/fake/usbipd.exe")) is None

    def test_exec_format_error_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WSL interop disabled: exec fails with errno 8 (Exec format error)."""

        def _raise(*a: Any, **k: Any) -> Any:
            raise OSError(8, "Exec format error")

        monkeypatch.setattr(wsl.subprocess, "run", _raise)
        assert wsl.list_devices(Path("/fake/usbipd.exe")) is None


# ---------------------------------------------------------------------------
# attach() -- the ONE mutating function
# ---------------------------------------------------------------------------


class TestAttach:
    def test_success_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="attached"),
        )
        ok, message = wsl.attach(Path("/fake/usbipd.exe"), "1-4")
        assert ok is True
        assert "attached" in message

    def test_failure_returns_false_with_stderr(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            wsl.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(returncode=1, stderr="device busy"),
        )
        ok, message = wsl.attach(Path("/fake/usbipd.exe"), "1-4")
        assert ok is False
        assert "device busy" in message

    def test_exception_returns_false_with_exception_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a: Any, **k: Any) -> Any:
            raise OSError("boom")

        monkeypatch.setattr(wsl.subprocess, "run", _raise)
        ok, message = wsl.attach(Path("/fake/usbipd.exe"), "1-4")
        assert ok is False
        assert "boom" in message

    def test_invokes_the_resolved_absolute_path_never_bare_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, list[str]] = {}

        def _run(argv: list[str], **kwargs: Any) -> Any:
            captured["argv"] = argv
            return _FakeCompletedProcess(returncode=0)

        monkeypatch.setattr(wsl.subprocess, "run", _run)
        wsl.attach(Path("/mnt/c/Program Files/usbipd-win/usbipd.exe"), "1-4")
        assert captured["argv"][0] == "/mnt/c/Program Files/usbipd-win/usbipd.exe"
        assert "--wsl" in captured["argv"]
        assert "1-4" in captured["argv"]


# ---------------------------------------------------------------------------
# wsl_conf_systemd_state()
# ---------------------------------------------------------------------------


class TestWslConfSystemdState:
    def test_absent_file(self, tmp_path: Path) -> None:
        state = wsl.wsl_conf_systemd_state(wsl_conf_path=tmp_path / "nonexistent")
        assert state == "absent"

    def test_boot_section_without_systemd(self, tmp_path: Path) -> None:
        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\ncommand = echo hi\n", encoding="utf-8")
        state = wsl.wsl_conf_systemd_state(wsl_conf_path=conf)
        assert state == "boot-section-exists"

    def test_systemd_true_is_enabled(self, tmp_path: Path) -> None:
        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=true\n", encoding="utf-8")
        state = wsl.wsl_conf_systemd_state(wsl_conf_path=conf)
        assert state == "enabled"

    def test_systemd_false_is_boot_section_exists(self, tmp_path: Path) -> None:
        conf = tmp_path / "wsl.conf"
        conf.write_text("[boot]\nsystemd=false\n", encoding="utf-8")
        state = wsl.wsl_conf_systemd_state(wsl_conf_path=conf)
        assert state == "boot-section-exists"

    def test_no_boot_section_at_all(self, tmp_path: Path) -> None:
        conf = tmp_path / "wsl.conf"
        conf.write_text("[network]\ngenerateResolvConf = false\n", encoding="utf-8")
        state = wsl.wsl_conf_systemd_state(wsl_conf_path=conf)
        assert state == "absent"

    def test_comments_are_ignored(self, tmp_path: Path) -> None:
        conf = tmp_path / "wsl.conf"
        conf.write_text(
            "[boot]\n# systemd=true (disabled for now)\nsystemd=true\n",
            encoding="utf-8",
        )
        state = wsl.wsl_conf_systemd_state(wsl_conf_path=conf)
        assert state == "enabled"
