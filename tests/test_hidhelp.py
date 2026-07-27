"""`muxplex_deck.hidhelp` -- table-driven over every state in the WSL cold-start
state machine (WSL_COLD_START_SPEC.md section 3).

Everything here is monkeypatched at the `usbnode`/`wsl` function level --
no real filesystem probing, no real subprocess, no real hardware. Asserts
on `Guidance.state` (the state-machine label) plus substring checks for
resolved BUSID/node values, and pins two invariants across every state:
no `<BUSID>`/`<NODE>` placeholder ever appears when the value was
resolvable, and every `usbipd` occurrence in printed text is `usbipd.exe`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from muxplex_deck import hidhelp, usbnode, wsl

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _wsl2_info() -> wsl.WslInfo:
    return wsl.WslInfo(
        is_wsl=True, version=2, kernel="5.15.90.1-microsoft-standard-WSL2"
    )


def _wsl1_info() -> wsl.WslInfo:
    return wsl.WslInfo(is_wsl=True, version=1, kernel="4.4.0-19041-Microsoft")


def _native_linux_info() -> wsl.WslInfo:
    return wsl.WslInfo(is_wsl=False, version=None, kernel="6.17.0-1014-nvidia")


def _paths(windows: str | None, impostor: str | None = None) -> wsl.UsbipdPaths:
    return wsl.UsbipdPaths(
        windows=Path(windows) if windows else None,
        linux_impostor=Path(impostor) if impostor else None,
    )


def _device(busid: str = "1-4", state: str = "not_shared") -> wsl.UsbipdDevice:
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


@pytest.fixture
def patch_env(monkeypatch: pytest.MonkeyPatch):
    """Returns a setter that patches every environment-probing function
    `explain_environment` touches, in one place.
    """

    def _set(
        *,
        info: wsl.WslInfo | None = None,
        udev_live: bool = True,
        paths: wsl.UsbipdPaths | None = None,
        devices: list[wsl.UsbipdDevice] | None = None,
        node: usbnode.UsbNode | None = None,
        systemd_state: str = "absent",
        platform: str = "linux",
    ) -> None:
        info = info if info is not None else _native_linux_info()
        paths = paths if paths is not None else _paths(None)
        monkeypatch.setattr(wsl, "detect", lambda **_kw: info)
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_kw: udev_live)
        monkeypatch.setattr(wsl, "find_usbipd", lambda: paths)
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: devices if devices is not None else []
        )
        monkeypatch.setattr(usbnode, "find_usb_node", lambda *a, **k: node)
        monkeypatch.setattr(wsl, "wsl_conf_systemd_state", lambda **_kw: systemd_state)
        monkeypatch.setattr(hidhelp.sys, "platform", platform)

    return _set


# ---------------------------------------------------------------------------
# Table-driven state coverage
# ---------------------------------------------------------------------------


class TestExplainEnvironmentStates:
    def test_w0_not_wsl_udev_live_returns_empty(self, patch_env) -> None:
        """The critical no-noise case: healthy native Linux/macOS."""
        patch_env(info=_native_linux_info(), udev_live=True)
        assert hidhelp.explain_environment() == []

    def test_w0_darwin_never_gets_u_dead_even_if_udev_check_would_fail(
        self, patch_env
    ) -> None:
        patch_env(info=_native_linux_info(), udev_live=False, platform="darwin")
        assert hidhelp.explain_environment() == []

    def test_u_dead_container_when_not_wsl_and_udev_not_live(self, patch_env) -> None:
        patch_env(info=_native_linux_info(), udev_live=False, platform="linux")
        guidances = hidhelp.explain_environment()
        assert len(guidances) == 1
        assert guidances[0].state == "U-DEAD"
        assert "not running" in guidances[0].message

    def test_w1_wsl1_is_fail(self, patch_env) -> None:
        patch_env(info=_wsl1_info())
        guidances = hidhelp.explain_environment()
        assert len(guidances) == 1
        assert guidances[0].state == "W1"
        assert guidances[0].status == "fail"

    def test_w2_usbipd_exe_absent(self, patch_env) -> None:
        patch_env(info=_wsl2_info(), paths=_paths(None))
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W2"
        assert "winget install" in guidances[-1].message

    def test_w2_when_list_devices_returns_none(self, patch_env, monkeypatch) -> None:
        patch_env(info=_wsl2_info(), paths=_paths("/mnt/c/usbipd.exe"))
        monkeypatch.setattr(wsl, "list_devices", lambda *a, **k: None)
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W2"

    def test_allow_usbipd_query_false_stops_before_querying(
        self, patch_env, monkeypatch
    ) -> None:
        patch_env(info=_wsl2_info(), paths=_paths("/mnt/c/usbipd.exe"))
        calls: list[str] = []
        monkeypatch.setattr(
            wsl,
            "list_devices",
            lambda *a, **k: calls.append("called") or [],
        )
        guidances = hidhelp.explain_environment(allow_usbipd_query=False)
        assert calls == []
        assert guidances == []

    def test_w3_no_matching_device(self, patch_env) -> None:
        patch_env(info=_wsl2_info(), paths=_paths("/mnt/c/usbipd.exe"), devices=[])
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W3"

    def test_w4_not_shared_has_real_busid(self, patch_env) -> None:
        device = _device(busid="1-4", state="not_shared")
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
        )
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W4"
        assert "1-4" in guidances[-1].message
        assert "<BUSID>" not in guidances[-1].message

    def test_w5_shared_has_real_busid(self, patch_env) -> None:
        device = _device(busid="1-4", state="shared")
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
        )
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W5"
        assert "1-4" in guidances[-1].message
        assert "wsl attach" in guidances[-1].message

    def test_w3_unknown_state_degrades_to_manual_instructions(self, patch_env) -> None:
        device = _device(busid="1-4", state="unknown")
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
        )
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W3"

    def test_w6_attached_but_node_not_visible(self, patch_env) -> None:
        device = _device(busid="1-4", state="attached")
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
            node=None,
        )
        guidances = hidhelp.explain_environment()
        assert guidances[-1].state == "W6"
        assert "1-4" in guidances[-1].message

    def test_w7_permission_wall_has_real_node_path(self, patch_env) -> None:
        device = _device(busid="1-4", state="attached")
        node = _node(readable_writable=False)
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
            node=node,
            udev_live=True,  # udev live -- no U-DEAD appended
        )
        guidances = hidhelp.explain_environment()
        assert len(guidances) == 1
        assert guidances[0].state == "W7"
        assert str(node.path) in guidances[0].message
        assert "<NODE>" not in guidances[0].message

    def test_w7_plus_u_dead_when_udev_also_not_live(self, patch_env) -> None:
        device = _device(busid="1-4", state="attached")
        node = _node(readable_writable=False)
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
            node=node,
            udev_live=False,
            systemd_state="absent",
        )
        guidances = hidhelp.explain_environment()
        states = [g.state for g in guidances]
        assert states == ["W7", "U-DEAD"]

    def test_w9_everything_works_returns_empty(self, patch_env) -> None:
        device = _device(busid="1-4", state="attached")
        node = _node(readable_writable=True)
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
            node=node,
        )
        assert hidhelp.explain_environment() == []

    def test_impostor_detected_when_both_binaries_resolve(self, patch_env) -> None:
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe", "/usr/bin/usbipd"),
            devices=[],
        )
        guidances = hidhelp.explain_environment()
        states = [g.state for g in guidances]
        assert "IMPOSTOR" in states

    def test_impostor_not_reported_when_only_windows_binary_present(
        self, patch_env
    ) -> None:
        patch_env(
            info=_wsl2_info(), paths=_paths("/mnt/c/usbipd.exe", None), devices=[]
        )
        guidances = hidhelp.explain_environment()
        states = [g.state for g in guidances]
        assert "IMPOSTOR" not in states


# ---------------------------------------------------------------------------
# Global invariants across every reachable state
# ---------------------------------------------------------------------------


class TestNoPlaceholdersAndUsbipdExeSpelling:
    def _all_wsl_guidance(self, patch_env) -> list[hidhelp.Guidance]:
        collected: list[hidhelp.Guidance] = []
        device = _device(busid="1-4", state="attached")
        node_visible_no_perm = _node(readable_writable=False)

        scenarios = [
            {"info": _wsl1_info()},
            {"info": _wsl2_info(), "paths": _paths(None)},
            {
                "info": _wsl2_info(),
                "paths": _paths("/mnt/c/usbipd.exe"),
                "devices": [],
            },
            {
                "info": _wsl2_info(),
                "paths": _paths("/mnt/c/usbipd.exe"),
                "devices": [_device(state="not_shared")],
            },
            {
                "info": _wsl2_info(),
                "paths": _paths("/mnt/c/usbipd.exe"),
                "devices": [_device(state="shared")],
            },
            {
                "info": _wsl2_info(),
                "paths": _paths("/mnt/c/usbipd.exe"),
                "devices": [device],
                "node": None,
            },
            {
                "info": _wsl2_info(),
                "paths": _paths("/mnt/c/usbipd.exe"),
                "devices": [device],
                "node": node_visible_no_perm,
                "udev_live": False,
            },
            {
                "info": _wsl2_info(),
                "paths": _paths("/mnt/c/usbipd.exe", "/usr/bin/usbipd"),
                "devices": [],
            },
        ]
        for scenario in scenarios:
            patch_env(**scenario)
            collected.extend(hidhelp.explain_environment())
        return collected

    def test_no_unresolved_busid_or_node_placeholder(self, patch_env) -> None:
        for guidance in self._all_wsl_guidance(patch_env):
            assert "<BUSID>" not in guidance.message
            assert "<NODE>" not in guidance.message

    def test_every_usbipd_mention_spells_dot_exe(self, patch_env) -> None:
        """Every bare `usbipd` occurrence must actually be `usbipd.exe` --
        never the ambiguous short form that risks the impostor trap.

        The IMPOSTOR message is exempt: its entire purpose is to show the
        Linux impostor's own (deliberately bare) path so the user can tell
        the two apart -- that occurrence isn't an instruction to type
        `usbipd`, it's a warning about what NOT to type.
        """
        import re

        # `(?!-win)` excludes the literal product name "usbipd-win" (the
        # Windows package to install) -- not a reference to the binary
        # itself, so it's exempt from the "always usbipd.exe" rule.
        for guidance in self._all_wsl_guidance(patch_env):
            if guidance.state == "IMPOSTOR":
                continue
            for match in re.finditer(r"usbipd(?!-win)(\.exe)?\b", guidance.message):
                assert match.group(1) == ".exe", (
                    f"bare 'usbipd' (not 'usbipd.exe') found in: {guidance.message!r}"
                )


# ---------------------------------------------------------------------------
# explain_open_failure
# ---------------------------------------------------------------------------


class TestExplainOpenFailure:
    def test_falls_back_when_environment_has_nothing_to_say(self, patch_env) -> None:
        patch_env(info=_native_linux_info(), udev_live=True)
        guidance = hidhelp.explain_open_failure("Permission denied")
        assert guidance.state == "W8"
        assert "Permission denied" in guidance.message

    def test_uses_environment_guidance_when_available(self, patch_env) -> None:
        device = _device(busid="1-4", state="not_shared")
        patch_env(
            info=_wsl2_info(),
            paths=_paths("/mnt/c/usbipd.exe"),
            devices=[device],
        )
        guidance = hidhelp.explain_open_failure("Could not open HID device.")
        assert guidance.state == "W4"
        assert "1-4" in guidance.message

    def test_does_not_query_usbipd_when_allow_usbipd_query_false(
        self, patch_env, monkeypatch
    ) -> None:
        patch_env(info=_wsl2_info(), paths=_paths("/mnt/c/usbipd.exe"))
        calls: list[str] = []
        monkeypatch.setattr(
            wsl, "list_devices", lambda *a, **k: calls.append("called") or []
        )
        hidhelp.explain_open_failure("boom", allow_usbipd_query=False)
        assert calls == []


# ---------------------------------------------------------------------------
# udev_guidance() -- P4: never printed when udev isn't live
# ---------------------------------------------------------------------------


class TestUdevGuidance:
    def test_returns_none_when_udev_not_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_kw: False)
        assert hidhelp.udev_guidance() is None

    def test_returns_guidance_when_udev_live(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(usbnode, "udev_is_live", lambda **_kw: True)
        guidance = hidhelp.udev_guidance()
        assert guidance is not None
        assert guidance.state == "U-LIVE"
        assert "plugdev" in guidance.message
        assert "hidraw" not in guidance.message  # V7: dead weight, dropped


# ---------------------------------------------------------------------------
# is_wsl2()
# ---------------------------------------------------------------------------


class TestIsWsl2:
    def test_true_on_wsl2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_kw: _wsl2_info())
        assert hidhelp.is_wsl2() is True

    def test_false_on_wsl1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_kw: _wsl1_info())
        assert hidhelp.is_wsl2() is False

    def test_false_on_native_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(wsl, "detect", lambda **_kw: _native_linux_info())
        assert hidhelp.is_wsl2() is False
