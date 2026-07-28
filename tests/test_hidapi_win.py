"""`muxplex_deck.hidapi_win` -- the vendored HIDAPI DLL load-path fix.

Everything here is monkeypatched: `sys.platform`, `vendored_dll_path()`,
`os.add_dll_directory` (which doesn't exist as a real attribute outside
Windows, hence `raising=False`), and `ctypes.util.find_library`. No real
DLL is loaded, no real registry/PATH state leaks -- `monkeypatch.setenv`
records and restores `PATH` even though production code mutates
`os.environ` directly rather than through monkeypatch (monkeypatch's
teardown restores the value it captured regardless of who wrote it in
between).
"""

from __future__ import annotations

import os

import pytest

from muxplex_deck import hidapi_win


@pytest.fixture(autouse=True)
def _reset_dll_directory_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    """Module-level registration-cookie global must not leak across tests."""
    monkeypatch.setattr(hidapi_win, "_dll_directory_cookie", None)


class TestEnsureHidapiNonWindows:
    def test_returns_none_on_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hidapi_win.sys, "platform", "linux")
        assert hidapi_win.ensure_hidapi() is None

    def test_touches_nothing_on_non_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PATH mutation, no add_dll_directory call, on a healthy non-Windows box."""
        monkeypatch.setattr(hidapi_win.sys, "platform", "darwin")
        calls: list[str] = []
        monkeypatch.setattr(
            hidapi_win.os,
            "add_dll_directory",
            lambda d: calls.append(d),
            raising=False,
        )
        monkeypatch.setenv("PATH", "/usr/bin")
        hidapi_win.ensure_hidapi()
        assert calls == []
        assert os.environ["PATH"] == "/usr/bin"


class TestEnsureHidapiWindows:
    def test_returns_none_when_vendored_dll_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(hidapi_win.sys, "platform", "win32")
        monkeypatch.setattr(
            hidapi_win, "vendored_dll_path", lambda: tmp_path / "absent" / "hidapi.dll"
        )
        calls: list[str] = []
        monkeypatch.setattr(
            hidapi_win.os,
            "add_dll_directory",
            lambda d: calls.append(d),
            raising=False,
        )
        assert hidapi_win.ensure_hidapi() is None
        assert calls == []

    def test_registers_dll_directory_and_prepends_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        dll_dir = tmp_path / "x64"
        dll_dir.mkdir()
        dll_path = dll_dir / "hidapi.dll"
        dll_path.write_bytes(b"not a real dll")

        monkeypatch.setattr(hidapi_win.sys, "platform", "win32")
        monkeypatch.setattr(hidapi_win, "vendored_dll_path", lambda: dll_path)
        recorded: list[str] = []
        monkeypatch.setattr(
            hidapi_win.os,
            "add_dll_directory",
            lambda d: recorded.append(d) or object(),
            raising=False,
        )
        monkeypatch.setenv("PATH", r"C:\Windows\System32")

        result = hidapi_win.ensure_hidapi()

        assert result == dll_dir
        assert recorded == [str(dll_dir)]
        assert os.environ["PATH"].startswith(str(dll_dir))
        assert r"C:\Windows\System32" in os.environ["PATH"]

    def test_idempotent_second_call_does_not_re_register_or_duplicate_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        dll_dir = tmp_path / "x64"
        dll_dir.mkdir()
        dll_path = dll_dir / "hidapi.dll"
        dll_path.write_bytes(b"not a real dll")

        monkeypatch.setattr(hidapi_win.sys, "platform", "win32")
        monkeypatch.setattr(hidapi_win, "vendored_dll_path", lambda: dll_path)
        recorded: list[str] = []
        monkeypatch.setattr(
            hidapi_win.os,
            "add_dll_directory",
            lambda d: recorded.append(d) or object(),
            raising=False,
        )
        monkeypatch.setenv("PATH", r"C:\Windows\System32")

        hidapi_win.ensure_hidapi()
        hidapi_win.ensure_hidapi()

        assert recorded == [str(dll_dir)], (
            "add_dll_directory must be called at most once (cookie guard)"
        )
        assert os.environ["PATH"].count(str(dll_dir)) == 1, (
            "PATH must not accumulate duplicate entries across repeated calls"
        )


class TestResolvedLibraryPath:
    def test_returns_none_on_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hidapi_win.sys, "platform", "linux")
        assert hidapi_win.resolved_library_path() is None

    def test_delegates_to_ctypes_find_library_on_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctypes.util

        monkeypatch.setattr(hidapi_win.sys, "platform", "win32")
        monkeypatch.setattr(
            ctypes.util, "find_library", lambda name: f"C:\\fake\\{name}.dll"
        )
        assert hidapi_win.resolved_library_path() == "C:\\fake\\hidapi.dll"

    def test_returns_none_when_find_library_finds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import ctypes.util

        monkeypatch.setattr(hidapi_win.sys, "platform", "win32")
        monkeypatch.setattr(ctypes.util, "find_library", lambda name: None)
        assert hidapi_win.resolved_library_path() is None


class TestVendoredDllPath:
    def test_points_at_expected_relative_location(self) -> None:
        path = hidapi_win.vendored_dll_path()
        assert path.parts[-4:] == ("_vendor", "hidapi", "x64", "hidapi.dll")

    def test_vendored_dll_actually_exists_in_this_checkout(self) -> None:
        """Not a platform check -- just confirms the binary was vendored,
        so a future accidental deletion is caught here instead of only on
        real Windows hardware.
        """
        assert hidapi_win.vendored_dll_path().exists()
