"""`muxplex-deck wsl attach` -- every state's exit code, and the load-bearing
invariant that `attach()` is never called in W4 (not shared) but always
called in W5 (shared, not yet attached).

All of `wsl`/`usbnode`'s module functions are monkeypatched directly --
`cli.wsl_attach()` imports them locally at call time (`from . import wsl as
wsl_mod`), which still binds to the same module object, so patching
`muxplex_deck.wsl.detect` etc. takes effect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muxplex_deck import cli, usbnode, wsl


def _wsl2_info() -> wsl.WslInfo:
    return wsl.WslInfo(
        is_wsl=True, version=2, kernel="5.15.90.1-microsoft-standard-WSL2"
    )


def _wsl1_info() -> wsl.WslInfo:
    return wsl.WslInfo(is_wsl=True, version=1, kernel="4.4.0-19041-Microsoft")


def _native_linux_info() -> wsl.WslInfo:
    return wsl.WslInfo(is_wsl=False, version=None, kernel="6.17.0-1014-nvidia")


def _paths(windows: str | None) -> wsl.UsbipdPaths:
    return wsl.UsbipdPaths(
        windows=Path(windows) if windows else None, linux_impostor=None
    )


def _device(busid: str = "1-4", state: str = "shared") -> wsl.UsbipdDevice:
    return wsl.UsbipdDevice(
        busid=busid, vid_pid="0fd9:006d", description="Elgato Stream Deck", state=state
    )


def _node(*, readable_writable: bool) -> usbnode.UsbNode:
    return usbnode.UsbNode(
        path=Path("/dev/bus/usb/001/003"),
        vendor_id="0fd9",
        product_id="006d",
        busnum=1,
        devnum=3,
        mode=0o660,
        owner_uid=0,
        readable_writable=readable_writable,
    )


class TestWslAttachExitCodes:
    def test_not_wsl_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _native_linux_info())
        assert cli.wsl_attach() == 1

    def test_wsl1_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl1_info())
        assert cli.wsl_attach() == 1

    def test_usbipd_exe_absent_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths(None))
        assert cli.wsl_attach() == 1

    def test_list_devices_none_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(wsl, "list_devices", lambda *a, **k: None)
        assert cli.wsl_attach() == 1

    def test_no_devices_found_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(wsl, "list_devices", lambda *a, **k: [])
        assert cli.wsl_attach() == 1

    def test_not_shared_exits_1_and_never_calls_attach(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attach_calls: list[tuple] = []
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="not_shared")]
        )
        monkeypatch.setattr(
            wsl, "attach", lambda *a, **k: attach_calls.append((a, k)) or (True, "")
        )

        result = cli.wsl_attach()

        assert result == 1
        assert attach_calls == []  # THE load-bearing invariant for W4

    def test_unknown_state_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="unknown")]
        )
        assert cli.wsl_attach() == 1

    def test_shared_calls_attach_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attach_calls: list[tuple] = []
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="shared")]
        )
        monkeypatch.setattr(
            wsl,
            "attach",
            lambda usbipd, busid, **k: (
                attach_calls.append((usbipd, busid)) or (True, "ok")
            ),
        )
        monkeypatch.setattr(
            usbnode, "find_usb_node", lambda *a, **k: _node(readable_writable=True)
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: True)

        result = cli.wsl_attach()

        assert result == 0
        assert len(attach_calls) == 1  # THE load-bearing invariant for W5
        assert attach_calls[0][1] == "1-4"

    def test_shared_attach_failure_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="shared")]
        )
        monkeypatch.setattr(wsl, "attach", lambda *a, **k: (False, "device busy"))

        assert cli.wsl_attach() == 1

    def test_already_attached_node_not_visible_exits_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="attached")]
        )
        monkeypatch.setattr(usbnode, "find_usb_node", lambda *a, **k: None)

        assert cli.wsl_attach() == 1

    def test_attached_node_visible_but_no_permission_exits_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attach itself succeeded -- a follow-up permission fix still
        needed does NOT change the exit code (spec section 7.8).
        """
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="attached")]
        )
        monkeypatch.setattr(
            usbnode, "find_usb_node", lambda *a, **k: _node(readable_writable=False)
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: True)

        assert cli.wsl_attach() == 0

    def test_already_attached_and_working_exits_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="attached")]
        )
        monkeypatch.setattr(
            usbnode, "find_usb_node", lambda *a, **k: _node(readable_writable=True)
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: True)

        assert cli.wsl_attach() == 0


class TestWslAttachSettlesBeforeDeclaringNotVisible:
    """Regression: a successful attach can return before sysfs has finished

    enumerating the device -- checking `find_usb_node` exactly once
    produced a false W6 ("not visible yet") moments before the very next
    `doctor` run found the device present and working. `wsl_attach()` must
    retry across a short, bounded settle window instead of failing on the
    very first miss.
    """

    def test_node_appears_on_second_check_succeeds_without_real_delay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(cli.time, "sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="shared")]
        )
        monkeypatch.setattr(wsl, "attach", lambda *a, **k: (True, "attached"))

        node_lookups: list[int] = []

        def _flaky_find_usb_node(*_a: object, **_k: object) -> usbnode.UsbNode | None:
            node_lookups.append(1)
            if len(node_lookups) < 2:
                return None
            return _node(readable_writable=True)

        monkeypatch.setattr(usbnode, "find_usb_node", _flaky_find_usb_node)
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: True)

        result = cli.wsl_attach()

        assert result == 0
        assert len(node_lookups) == 2
        assert len(sleep_calls) == 1  # exactly one bounded settle wait

    def test_node_never_appears_still_exits_1_after_bounded_retries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sleep_calls: list[float] = []
        monkeypatch.setattr(cli.time, "sleep", lambda s: sleep_calls.append(s))
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(state="shared")]
        )
        monkeypatch.setattr(wsl, "attach", lambda *a, **k: (True, "attached"))
        monkeypatch.setattr(usbnode, "find_usb_node", lambda *a, **k: None)

        result = cli.wsl_attach()

        assert result == 1
        # Bounded, not infinite -- exactly the configured settle window.
        assert len(sleep_calls) == cli._ATTACH_SETTLE_ATTEMPTS - 1


class TestWslAttachOutputContent:
    def test_success_prints_real_busid_and_node(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_k: _wsl2_info())
        monkeypatch.setattr(wsl, "find_usbipd", lambda: _paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: [_device(busid="1-4", state="shared")]
        )
        monkeypatch.setattr(wsl, "attach", lambda *a, **k: (True, "attached"))
        monkeypatch.setattr(
            usbnode, "find_usb_node", lambda *a, **k: _node(readable_writable=True)
        )
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_k: True)

        cli.wsl_attach()

        out = capsys.readouterr().out
        assert "1-4" in out
        assert "/dev/bus/usb/001/003" in out
        assert "<BUSID>" not in out
        assert "<NODE>" not in out
