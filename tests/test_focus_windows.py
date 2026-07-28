"""`muxplex_deck.focus`'s Windows foreground-focus path.

Real user feedback (Windows): "I don't have [the] popping of Muxplex PWA
to the foreground when selecting a session via the Stream Deck." macOS's
`open -a <name>` has no Windows analog -- the PWA runs inside a generic
browser process there, addressable only by its window TITLE -- so these
tests exercise `_focus_windows` against a fake `_Win32Api` (a
`_FakeWin32Api` below), never real `ctypes.windll`/`ctypes.WINFUNCTYPE`
(both are genuinely absent as attributes outside a real Windows process;
monkeypatching `sys.platform` does not change that). `focus.focus_app` is
the public entry point exercised throughout, with `sys.platform`
monkeypatched to `"win32"` and `focus._real_win32_api` monkeypatched to
return the fake -- proving the real dispatch path in `focus_app`, not just
the private helpers in isolation.
"""

from __future__ import annotations

import ctypes

import pytest

from muxplex_deck import focus


class _FakeWin32Api:
    """In-memory stand-in for `focus._Win32Api` -- no real window APIs."""

    def __init__(
        self,
        *,
        windows: list[tuple[int, str]] | None = None,
        foreground_hwnd: int = 0,
        foreground_thread_id: int = 0,
        current_thread_id: int = 999,
        iconic: set[int] | None = None,
        attach_thread_input_result: bool = True,
        set_foreground_result: bool = True,
        confirm_switch: bool = True,
    ) -> None:
        self._windows = windows or []
        self._foreground_hwnd = foreground_hwnd
        self._foreground_thread_id = foreground_thread_id
        self._current_thread_id = current_thread_id
        self._iconic = iconic or set()
        self._attach_thread_input_result = attach_thread_input_result
        self._set_foreground_result = set_foreground_result
        # After a successful SetForegroundWindow call, does
        # GetForegroundWindow() report the target hwnd? (Simulates the
        # OS actually honoring vs. merely flashing the taskbar.)
        self._confirm_switch = confirm_switch
        self._target_hwnd: int | None = None

        self.restored: list[int] = []
        self.attach_calls: list[tuple[int, int, bool]] = []
        self.set_foreground_calls: list[int] = []
        self.alt_tap_calls = 0

    def tap_alt_key(self) -> None:
        self.alt_tap_calls += 1

    def list_window_titles(self) -> list[tuple[int, str]]:
        return list(self._windows)

    def is_iconic(self, hwnd: int) -> bool:
        return hwnd in self._iconic

    def restore(self, hwnd: int) -> None:
        self.restored.append(hwnd)
        self._iconic.discard(hwnd)

    def get_foreground_window(self) -> int:
        if self._target_hwnd is not None and self._confirm_switch:
            return self._target_hwnd
        return self._foreground_hwnd

    def get_window_thread_id(self, hwnd: int) -> int:
        if hwnd == self._foreground_hwnd:
            return self._foreground_thread_id
        return 0

    def get_current_thread_id(self) -> int:
        return self._current_thread_id

    def attach_thread_input(self, this_id: int, other_id: int, attach: bool) -> bool:
        self.attach_calls.append((this_id, other_id, attach))
        return self._attach_thread_input_result

    def set_foreground_window(self, hwnd: int) -> bool:
        self.set_foreground_calls.append(hwnd)
        self._target_hwnd = hwnd
        return self._set_foreground_result


@pytest.fixture(autouse=True)
def _reset_unsupported_logged_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_unsupported_logged` is a module-level global -- must not leak
    between tests (mirrors the existing macOS test suite's own hygiene)."""
    monkeypatch.setattr(focus, "_unsupported_logged", False)


def _use_windows(monkeypatch: pytest.MonkeyPatch, api: _FakeWin32Api) -> None:
    monkeypatch.setattr(focus.sys, "platform", "win32")
    monkeypatch.setattr(focus, "_real_win32_api", lambda: api)


class TestFocusAppDispatchesToWindows:
    def test_empty_name_is_a_pure_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        api = _FakeWin32Api()
        _use_windows(monkeypatch, api)
        focus.focus_app("")
        assert api.set_foreground_calls == []
        assert api.list_window_titles() == []  # never even enumerated

    def test_darwin_still_takes_precedence_over_windows_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Platform dispatch order is unaffected by adding Windows support."""
        monkeypatch.setattr(focus.sys, "platform", "darwin")
        called: list[str] = []
        monkeypatch.setattr(focus, "_focus_windows", lambda name: called.append(name))
        monkeypatch.setattr(
            focus.subprocess,
            "run",
            lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})(),
        )
        focus.focus_app("muxplex")
        assert called == []


class TestFindAndFocusWindow:
    def test_matches_window_title_case_insensitively(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeWin32Api(
            windows=[(1, "Inbox - Outlook"), (2, "muxplex - Microsoft Edge")],
            foreground_hwnd=1,
            foreground_thread_id=42,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("MUXPLEX")
        assert api.set_foreground_calls == [2]

    def test_no_matching_window_logs_and_does_not_call_set_foreground(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = _FakeWin32Api(windows=[(1, "Inbox - Outlook")])
        _use_windows(monkeypatch, api)
        with caplog.at_level("WARNING", logger="muxplex_deck"):
            focus.focus_app("muxplex")
        assert api.set_foreground_calls == []
        assert any("no window found" in r.message for r in caplog.records)

    def test_minimized_window_is_restored_before_foregrounding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
            iconic={7},
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.restored == [7]
        assert api.set_foreground_calls == [7]

    def test_already_active_session_still_reissues_foreground_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per the real user's reported usage: pressing the already-active
        session key must still re-focus, not silently no-op."""
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=7,
            foreground_thread_id=42,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.set_foreground_calls == [7]

    def test_attaches_and_detaches_thread_input_around_the_foreground_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
            current_thread_id=99,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.attach_calls == [(99, 42, True), (99, 42, False)]

    def test_no_attach_when_current_thread_already_owns_foreground(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same thread -- AttachThreadInput would be a no-op; skip it."""
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=99,
            current_thread_id=99,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.attach_calls == []


class TestAltKeyTap:
    """The 2026-07-28 fix: `AttachThreadInput` alone was proven insufficient

    on real hardware (Windows only flashed the taskbar icon). `tap_alt_key()`
    -- a `SendInput`-synthesized ALT keydown/keyup -- is Microsoft's own
    documented mechanism for re-enabling `SetForegroundWindow`
    (`LockSetForegroundWindow`'s Remarks) and must run on every focus
    attempt, before `AttachThreadInput`/`SetForegroundWindow`.
    """

    def test_alt_tap_runs_exactly_once_per_focus_attempt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.alt_tap_calls == 1

    def test_alt_tap_runs_even_when_no_attach_is_needed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same-thread case (no AttachThreadInput) must still tap ALT --

        it is an independent, unconditional step, not a fallback only used
        when AttachThreadInput would apply.
        """
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=99,
            current_thread_id=99,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.alt_tap_calls == 1

    def test_alt_tap_runs_before_set_foreground_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order: list[str] = []

        class _OrderTrackingApi(_FakeWin32Api):
            def tap_alt_key(self) -> None:
                order.append("alt_tap")
                super().tap_alt_key()

            def set_foreground_window(self, hwnd: int) -> bool:
                order.append("set_foreground")
                return super().set_foreground_window(hwnd)

        api = _OrderTrackingApi(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
        )
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert order == ["alt_tap", "set_foreground"]

    def test_no_window_found_never_taps_alt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing to focus -- don't synthesize input for no reason."""
        api = _FakeWin32Api(windows=[(1, "Inbox - Outlook")])
        _use_windows(monkeypatch, api)
        focus.focus_app("muxplex")
        assert api.alt_tap_calls == 0


class TestForegroundConfirmationIsHonest:
    """The module's central honesty claim: a `SetForegroundWindow` call
    that Windows silently downgrades to a taskbar flash must be reported
    as such, never as if the switch worked.
    """

    def test_confirmed_switch_logs_nothing_at_warning_or_above(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
            confirm_switch=True,
        )
        _use_windows(monkeypatch, api)
        with caplog.at_level("INFO", logger="muxplex_deck"):
            focus.focus_app("muxplex")
        assert not any(r.levelname in ("WARNING", "ERROR") for r in caplog.records)

    def test_unconfirmed_switch_degrades_to_a_logged_notice_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = _FakeWin32Api(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
            confirm_switch=False,
        )
        _use_windows(monkeypatch, api)
        with caplog.at_level("INFO", logger="muxplex_deck"):
            focus.focus_app("muxplex")  # must not raise
        assert any(
            "did not" in r.message and "confirm" in r.message for r in caplog.records
        )


class TestInputStructureLayout:
    """Pure ctypes layout checks for `SendInput`'s `INPUT` structure --

    no `ctypes.windll` involved, so these run on any platform (unlike
    `_RealWin32Api`, which this module's own docstring says tests never
    construct). `ctypes.wintypes` is a plain-data module importable
    anywhere; only `ctypes.windll` (the actual DLL loader) is Windows-only.
    """

    def test_input_union_is_large_enough_for_mouseinput(self) -> None:
        """MOUSEINPUT is the largest union member on real Windows -- if the

        union were sized to KEYBDINPUT alone, `cbSize` would mismatch what
        `SendInput` expects and the real call would be rejected.
        """
        assert ctypes.sizeof(focus._INPUTUnion) >= ctypes.sizeof(focus._MOUSEINPUT)
        assert ctypes.sizeof(focus._INPUTUnion) >= ctypes.sizeof(focus._KEYBDINPUT)

    def test_keybdinput_can_be_constructed_for_alt_keydown_and_keyup(self) -> None:
        down = focus._KEYBDINPUT(
            wVk=focus._VK_MENU, wScan=0, dwFlags=0, time=0, dwExtraInfo=None
        )
        up = focus._KEYBDINPUT(
            wVk=focus._VK_MENU,
            wScan=0,
            dwFlags=focus._KEYEVENTF_KEYUP,
            time=0,
            dwExtraInfo=None,
        )
        assert down.wVk == up.wVk == focus._VK_MENU
        assert down.dwFlags == 0
        assert up.dwFlags == focus._KEYEVENTF_KEYUP

    def test_input_array_of_two_assigns_type_and_union_independently(self) -> None:
        inputs = (focus._INPUT * 2)()
        inputs[0].type = focus._INPUT_KEYBOARD
        inputs[0].union.ki = focus._KEYBDINPUT(
            wVk=focus._VK_MENU, wScan=0, dwFlags=0, time=0, dwExtraInfo=None
        )
        inputs[1].type = focus._INPUT_KEYBOARD
        inputs[1].union.ki = focus._KEYBDINPUT(
            wVk=focus._VK_MENU,
            wScan=0,
            dwFlags=focus._KEYEVENTF_KEYUP,
            time=0,
            dwExtraInfo=None,
        )
        assert inputs[0].type == focus._INPUT_KEYBOARD
        assert inputs[0].union.ki.dwFlags == 0
        assert inputs[1].union.ki.dwFlags == focus._KEYEVENTF_KEYUP


class TestNeverRaises:
    def test_oserror_from_the_api_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        class _RaisingApi(_FakeWin32Api):
            def set_foreground_window(self, hwnd: int) -> bool:
                raise OSError("boom")

        api = _RaisingApi(
            windows=[(7, "muxplex - Chrome")],
            foreground_hwnd=1,
            foreground_thread_id=42,
        )
        _use_windows(monkeypatch, api)
        with caplog.at_level("WARNING", logger="muxplex_deck"):
            focus.focus_app("muxplex")  # must not raise
        assert any("could not activate" in r.message for r in caplog.records)
