"""`main._FailureEpisode` -- log-once-per-episode discipline, and the
status-file fix for the open-failure branch (WSL_COLD_START_SPEC.md section 9).

No hardware, no real device, no real subprocess: `main.run()` is driven with
fake `DeviceManager`/`DeckDevice` objects and a real `StatusReporter`
pointed at a `tmp_path` status file (the autouse `XDG_STATE_HOME` rail
would redirect it there anyway).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import main as main_mod
from muxplex_deck import statusfile
from muxplex_deck.config import Config

# ---------------------------------------------------------------------------
# _configure_logging -- log_file plumbing for Windows Task Scheduler
# (WINDOWS_NATIVE_SPEC.md section 1.5): pythonw.exe leaves sys.stdout/
# sys.stderr as None, so a --log-file path must route logging to a
# RotatingFileHandler instead of a StreamHandler. On macOS/Linux (no
# log_file, real stderr) behavior stays byte-for-byte the same as before
# this parameter existed.
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_log_file_writes_via_rotating_file_handler(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_path = tmp_path / "nested" / "muxplex-deck.log"
        # A previous test in this process may have already called
        # logging.basicConfig(); reset the root logger's handlers so this
        # test's basicConfig() call actually takes effect.
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        main_mod._configure_logging(log_path)
        logging.getLogger("muxplex_deck").info("hello from the log file")
        for h in root.handlers:
            h.flush()

        assert log_path.exists()
        assert "hello from the log file" in log_path.read_text(encoding="utf-8")

    def test_no_log_file_and_no_stderr_does_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Defensive: a console-less launch (pythonw.exe) with no --log-file

        must not crash trying to build a StreamHandler around a None stream.
        """
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)
        monkeypatch.setattr(main_mod.sys, "stderr", None)

        main_mod._configure_logging(None)  # must not raise
        logging.getLogger("muxplex_deck").info("should not crash")

    def test_no_log_file_with_real_stderr_is_unchanged(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """macOS/Linux regression guard: default (no log_file) behavior is

        byte-for-byte the same as before `log_file` existed.
        """
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        main_mod._configure_logging(None)
        logging.getLogger("muxplex_deck").warning("plain stderr message")

        captured = capsys.readouterr()
        assert "plain stderr message" in captured.err


# ---------------------------------------------------------------------------
# _build_log_file_handler -- must never raise, must never take the whole
# process down when the log file can't be opened as intended. Before this
# fix, an unguarded RotatingFileHandler construction failure crashed run()
# before a single line could be logged anywhere -- under pythonw.exe (no
# console) that means the sidecar dies with ZERO diagnostic.
# ---------------------------------------------------------------------------


class TestBuildLogFileHandlerFallback:
    def test_normal_case_returns_a_working_rotating_handler(
        self, tmp_path: Path
    ) -> None:
        from logging.handlers import RotatingFileHandler

        log_path = tmp_path / "nested" / "muxplex-deck.log"
        handler = main_mod._build_log_file_handler(log_path)
        try:
            assert isinstance(handler, RotatingFileHandler)
            assert log_path.exists()
        finally:
            handler.close()

    def test_falls_back_to_plain_file_handler_when_rotating_construction_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging.handlers as logging_handlers

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated: locked by a concurrent writer")

        monkeypatch.setattr(logging_handlers, "RotatingFileHandler", _boom)

        log_path = tmp_path / "muxplex-deck.log"
        handler = main_mod._build_log_file_handler(log_path)
        try:
            assert isinstance(handler, logging.FileHandler)
            assert log_path.exists()
        finally:
            handler.close()

    def test_falls_back_to_stderr_when_both_file_handlers_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging.handlers as logging_handlers

        real_file_handler_cls = logging.FileHandler  # capture before patching it away

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated failure")

        monkeypatch.setattr(logging_handlers, "RotatingFileHandler", _boom)
        monkeypatch.setattr(main_mod.logging, "FileHandler", _boom)

        handler = main_mod._build_log_file_handler(tmp_path / "muxplex-deck.log")
        assert isinstance(handler, logging.StreamHandler)
        assert not isinstance(handler, real_file_handler_cls)

    def test_falls_back_to_null_handler_when_stderr_is_also_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging.handlers as logging_handlers

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated failure")

        monkeypatch.setattr(logging_handlers, "RotatingFileHandler", _boom)
        monkeypatch.setattr(main_mod.logging, "FileHandler", _boom)
        monkeypatch.setattr(main_mod.sys, "stderr", None)

        handler = main_mod._build_log_file_handler(tmp_path / "muxplex-deck.log")
        assert isinstance(handler, logging.NullHandler)

    def test_configure_logging_never_raises_when_the_log_file_cannot_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import logging.handlers as logging_handlers

        def _boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated: disk full")

        monkeypatch.setattr(logging_handlers, "RotatingFileHandler", _boom)
        monkeypatch.setattr(main_mod.logging, "FileHandler", _boom)

        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)

        main_mod._configure_logging(tmp_path / "muxplex-deck.log")  # must not raise


# ---------------------------------------------------------------------------
# _FailureEpisode -- the reusable log-once-per-episode tracker
# ---------------------------------------------------------------------------


class TestFailureEpisode:
    def test_first_failure_logs_error_and_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        episode = main_mod._FailureEpisode(heartbeat_seconds=9999.0)
        with caplog.at_level(logging.DEBUG, logger="muxplex_deck"):
            episode.note(
                ValueError("boom"),
                build_detail=lambda: "detail text",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(error_records) == 1
        assert "detail text" in error_records[0].message
        assert len(debug_records) == 1

    def test_repeated_same_signature_is_silent_until_heartbeat(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        episode = main_mod._FailureEpisode(heartbeat_seconds=9999.0)
        build_calls = 0

        def _build() -> str:
            nonlocal build_calls
            build_calls += 1
            return "detail"

        with caplog.at_level(logging.DEBUG, logger="muxplex_deck"):
            for _ in range(50):
                episode.note(
                    ValueError("boom"),
                    build_detail=_build,
                    error_prefix="failed: ",
                    heartbeat_message="still failing (attempt %d)",
                )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        # Exactly one ERROR (the episode's first failure); expensive
        # build_detail() must run exactly once, not once per call.
        assert len(error_records) == 1
        assert build_calls == 1
        # Heartbeat threshold (9999s) never elapses across this loop.
        assert info_records == []

    def test_heartbeat_fires_after_threshold_elapses(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        episode = main_mod._FailureEpisode(heartbeat_seconds=10.0)
        fake_now = [0.0]
        monkeypatch.setattr(main_mod.time, "monotonic", lambda: fake_now[0])

        with caplog.at_level(logging.INFO, logger="muxplex_deck"):
            episode.note(
                ValueError("boom"),
                build_detail=lambda: "d",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )
            fake_now[0] = 11.0
            episode.note(
                ValueError("boom"),
                build_detail=lambda: "d",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )

        info_records = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_records) == 1
        assert "attempt 2" in info_records[0].message

    def test_changed_signature_starts_a_new_episode(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        episode = main_mod._FailureEpisode(heartbeat_seconds=9999.0)
        with caplog.at_level(logging.ERROR, logger="muxplex_deck"):
            episode.note(
                ValueError("first error"),
                build_detail=lambda: "d1",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )
            episode.note(
                ValueError("second error"),
                build_detail=lambda: "d2",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 2

    def test_reset_starts_a_fresh_episode(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        episode = main_mod._FailureEpisode(heartbeat_seconds=9999.0)
        with caplog.at_level(logging.ERROR, logger="muxplex_deck"):
            episode.note(
                ValueError("boom"),
                build_detail=lambda: "d",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )
            episode.reset()
            episode.note(
                ValueError("boom"),
                build_detail=lambda: "d",
                error_prefix="failed: ",
                heartbeat_message="still failing (attempt %d)",
            )
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_records) == 2


# ---------------------------------------------------------------------------
# main.run() -- 50 simulated open failures produce exactly one traceback +
# the status file gets a hint (fixes V6: the false "Server: unreachable").
# ---------------------------------------------------------------------------


class _AlwaysFailsToOpenDeck:
    def open(self) -> None:
        raise RuntimeError("Could not open HID device.")

    def is_open(self) -> bool:
        return False

    def connected(self) -> bool:
        return False

    def key_count(self) -> int:
        return 1


class _FakeManagerAlwaysFindsDevice:
    def __init__(self, deck: Any) -> None:
        self._deck = deck

    def find_device(self) -> Any:
        return self._deck


def _make_config(tmp_path: Path) -> Config:
    return Config(
        server_url="https://example.test:8088",
        federation_key="fake-key",
        ca_file=None,
        poll_interval=2.0,
        sort="attention",
        controls={},
        focus_app="",
    )


class TestOpenFailureLoggingAndStatusFile:
    def test_open_failure_updates_status_file_with_hint_and_clears_server_unreachable_illusion(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """This is the V6 regression test: before the fix, the status file
        never got touched on an open failure, so `status` reported a stale
        "Server: unreachable" even though the server was never contacted.
        """
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            hidhelp,
            "explain_open_failure",
            lambda error, **k: hidhelp.Guidance(
                status="warn", message="fake guidance for testing", state="W7"
            ),
        )

        status_path = tmp_path / "status.json"
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        shutting_down = threading.Event()
        deck = _AlwaysFailsToOpenDeck()
        manager = _FakeManagerAlwaysFindsDevice(deck)
        config = _make_config(tmp_path)

        # Run just a couple of iterations then request shutdown -- we only
        # need to observe the status file after at least one failed open.
        call_count = {"n": 0}
        real_wait = shutting_down.wait

        def _wait(seconds: float) -> bool:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                shutting_down.set()
            return real_wait(0)

        monkeypatch.setattr(shutting_down, "wait", _wait)
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: shutting_down)

        main_mod.run(config, manager)

        data = statusfile.read_status(status_path)
        assert data is not None
        assert data["device"]["connected"] is False
        assert data["device"].get("hint") == "fake guidance for testing"
        # The server was never contacted -- must NOT claim unreachable.
        assert data["server"]["connected"] is False
        assert data["server"]["last_error"] is None

    def test_fifty_open_failures_produce_exactly_one_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from muxplex_deck import hidhelp

        query_calls = {"n": 0}

        def _fake_explain(error: str, **kwargs: object) -> hidhelp.Guidance:
            query_calls["n"] += 1
            return hidhelp.Guidance(status="warn", message="guidance", state="W7")

        monkeypatch.setattr(hidhelp, "explain_open_failure", _fake_explain)
        monkeypatch.setattr(
            statusfile, "default_status_path", lambda: tmp_path / "status.json"
        )

        shutting_down = threading.Event()
        deck = _AlwaysFailsToOpenDeck()
        manager = _FakeManagerAlwaysFindsDevice(deck)
        config = _make_config(tmp_path)

        call_count = {"n": 0}
        real_wait = shutting_down.wait

        def _wait(seconds: float) -> bool:
            call_count["n"] += 1
            if call_count["n"] >= 50:
                shutting_down.set()
            return real_wait(0)

        monkeypatch.setattr(shutting_down, "wait", _wait)
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: shutting_down)

        with caplog.at_level(logging.DEBUG, logger="muxplex_deck"):
            main_mod.run(config, manager)

        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and r.exc_info is not None
        ]
        error_records = [
            r
            for r in caplog.records
            if r.levelno == logging.ERROR and "cannot open the Stream Deck" in r.message
        ]
        assert len(debug_records) == 1
        assert len(error_records) == 1
        # explain_open_failure (which may shell out to usbipd.exe on WSL)
        # must be called exactly once per episode, not once per cycle.
        assert query_calls["n"] == 1


# ---------------------------------------------------------------------------
# run() + the single-instance guard -- the fix for the Windows Task
# Scheduler double-spawn incident (AGENTS.md / WINDOWS_NATIVE_SPEC.md): a
# single `schtasks /Run` produced two live sidecars on real hardware. This
# guard makes a second instance detect the first and exit cleanly instead
# of racing it for the exclusive HID handle and the shared status file --
# regardless of platform, and regardless of what triggers the duplicate
# launch (Task Scheduler, a manual `run` while the service is already up,
# a stale process, a double-click).
# ---------------------------------------------------------------------------


class _NeverFindsDevice:
    def find_device(self) -> Any:
        return None


class TestRunSingleInstanceGuard:
    def test_second_instance_exits_cleanly_without_touching_the_device(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from muxplex_deck.singleton import InstanceLock

        lock_path = tmp_path / "muxplex-deck.lock"
        holder = InstanceLock(lock_path)
        holder.acquire()
        try:
            config = _make_config(tmp_path)
            find_device_calls = {"n": 0}

            class _CountingManager:
                def find_device(self) -> Any:
                    find_device_calls["n"] += 1
                    return None

            with caplog.at_level(logging.ERROR, logger="muxplex_deck"):
                result = main_mod.run(config, _CountingManager(), lock_path=lock_path)

            assert result == 1
            # The whole point of the guard: a losing instance must never
            # reach the device/server logic at all, not even once.
            assert find_device_calls["n"] == 0
            assert any(
                "already running" in r.message
                for r in caplog.records
                if r.levelno == logging.ERROR
            )
        finally:
            holder.release()

    def test_lock_is_released_after_run_returns_so_a_replacement_can_start(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The v0.5.3 restart contract: once `run()` returns (any exit

        path), a replacement instance must acquire the lock immediately --
        no stale-lock window that would wedge a legitimate restart.
        """
        lock_path = tmp_path / "muxplex-deck.lock"
        config = _make_config(tmp_path)

        already_done = threading.Event()
        already_done.set()  # loop body never runs -- exits on the first check
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: already_done)

        result = main_mod.run(config, _NeverFindsDevice(), lock_path=lock_path)
        assert result == 0

        # A second, independent run() against the SAME lock path must
        # succeed immediately -- this is the replacement-instance handoff.
        still_done = threading.Event()
        still_done.set()
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: still_done)
        result2 = main_mod.run(config, _NeverFindsDevice(), lock_path=lock_path)
        assert result2 == 0

    def test_lock_is_released_when_the_loop_raises_unexpectedly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even a crash inside the loop must release the lock -- a bug in

        the hotplug loop must not also permanently wedge every future
        restart behind a lock nobody will ever release.
        """
        lock_path = tmp_path / "muxplex-deck.lock"
        config = _make_config(tmp_path)

        def _boom() -> Any:
            raise RuntimeError("simulated crash installing the signal handler")

        monkeypatch.setattr(main_mod, "_install_signal_handler", _boom)

        with pytest.raises(RuntimeError):
            main_mod.run(config, _NeverFindsDevice(), lock_path=lock_path)

        from muxplex_deck.singleton import InstanceLock

        # The lock must already be free again.
        probe = InstanceLock(lock_path)
        probe.acquire()  # must not raise
        probe.release()

    def test_default_lock_path_is_used_when_none_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: `run()` must actually wire `lock_path=None` to

        `singleton.default_lock_path()`, not silently skip locking.
        """
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        config = _make_config(tmp_path)

        already_done = threading.Event()
        already_done.set()
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: already_done)

        result = main_mod.run(config, _NeverFindsDevice())
        assert result == 0

        expected_lock_path = tmp_path / "state" / "muxplex-deck" / "muxplex-deck.lock"
        assert expected_lock_path.exists()


# ---------------------------------------------------------------------------
# _shutdown_cleanup / device blanking on shutdown -- real user report:
# `muxplex-deck service stop` stopped the process but the deck's LCD keys
# kept showing the last-painted session state indefinitely. Fires on every
# `_run()` exit path (SIGTERM/SIGINT via `_install_signal_handler`, or a
# normal/exceptional return) via `_run()`'s own outermost `finally` --
# independent of whether a device happened to be mid-active-session.
# Never fires on a hard kill (SIGKILL, Windows TerminateProcess) -- there is
# no Python hook point for that, and none is faked here.
# ---------------------------------------------------------------------------


class _RecordingDeck:
    """Minimal fake `DeckDevice` that records reset()/close() calls and can

    simulate an error from either, without ever raising past _safe_close.
    """

    def __init__(
        self,
        *,
        open_state: bool = True,
        raise_on_reset: Exception | None = None,
        raise_on_close: Exception | None = None,
    ) -> None:
        self._open = open_state
        self._raise_on_reset = raise_on_reset
        self._raise_on_close = raise_on_close
        self.reset_calls = 0
        self.close_calls = 0

    def is_open(self) -> bool:
        return self._open

    def connected(self) -> bool:
        return self._open

    def reset(self) -> None:
        self.reset_calls += 1
        if self._raise_on_reset is not None:
            raise self._raise_on_reset

    def close(self) -> None:
        self.close_calls += 1
        self._open = False
        if self._raise_on_close is not None:
            raise self._raise_on_close


class TestShutdownCleanup:
    def test_none_deck_is_a_safe_no_op(self) -> None:
        main_mod._shutdown_cleanup(None)  # must not raise

    def test_open_deck_is_reset_and_closed(self) -> None:
        deck = _RecordingDeck(open_state=True)
        main_mod._shutdown_cleanup(deck)  # type: ignore[arg-type]
        assert deck.reset_calls == 1
        assert deck.close_calls == 1

    def test_already_closed_deck_skips_reset_but_still_closes(self) -> None:
        """`_safe_close`'s existing `is_open()` guard: a device that was

        already reset+closed earlier (e.g. by `_run_active`'s own
        finally) must not be reset a second time, but close() is still
        called -- harmless on an already-closed handle.
        """
        deck = _RecordingDeck(open_state=False)
        main_mod._shutdown_cleanup(deck)  # type: ignore[arg-type]
        assert deck.reset_calls == 0
        assert deck.close_calls == 1

    def test_reset_error_is_swallowed_and_close_still_runs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        deck = _RecordingDeck(
            open_state=True, raise_on_reset=RuntimeError("device unplugged")
        )
        with caplog.at_level(logging.ERROR, logger="muxplex_deck"):
            main_mod._shutdown_cleanup(deck)  # type: ignore[arg-type] -- must not raise
        assert deck.close_calls == 1
        assert any(
            "Unexpected error while resetting deck" in r.message for r in caplog.records
        )

    def test_close_error_is_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        deck = _RecordingDeck(
            open_state=True, raise_on_close=RuntimeError("handle already gone")
        )
        with caplog.at_level(logging.ERROR, logger="muxplex_deck"):
            main_mod._shutdown_cleanup(deck)  # type: ignore[arg-type] -- must not raise
        assert deck.reset_calls == 1
        assert any(
            "Unexpected error while closing deck" in r.message for r in caplog.records
        )

    def test_a_completely_broken_device_never_raises_out_of_shutdown(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Defense in depth: even if `is_open()` itself blows up (a future

        backend bug, or a device object in a genuinely broken state), the
        shutdown path must still complete cleanly -- never make shutdown
        itself the thing that crashes.
        """

        class _ExplodingDeck:
            def is_open(self) -> bool:
                raise RuntimeError("completely broken")

            def close(self) -> None:
                raise RuntimeError("also broken")

        with caplog.at_level(logging.ERROR, logger="muxplex_deck"):
            main_mod._shutdown_cleanup(_ExplodingDeck())  # type: ignore[arg-type] -- must not raise


class TestRunCallsShutdownCleanupOnEveryExitPath:
    """Integration: `main.run()` -> `_run()` actually wires `current_deck`

    through to `_shutdown_cleanup` on the way out, regardless of which
    branch was executing when `shutting_down` was set.
    """

    def test_shutdown_while_device_absent_is_a_no_op(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No device was ever found -- nothing to blank, and no crash."""
        monkeypatch.setattr(
            statusfile, "default_status_path", lambda: tmp_path / "status.json"
        )
        config = _make_config(tmp_path)

        shutting_down = threading.Event()
        call_count = {"n": 0}

        def _wait(seconds: float) -> bool:
            call_count["n"] += 1
            if call_count["n"] >= 2:
                shutting_down.set()
            return False

        monkeypatch.setattr(shutting_down, "wait", _wait)
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: shutting_down)

        cleanup_calls: list[Any] = []
        real_cleanup = main_mod._shutdown_cleanup

        def _spy(deck: Any) -> None:
            cleanup_calls.append(deck)
            real_cleanup(deck)

        monkeypatch.setattr(main_mod, "_shutdown_cleanup", _spy)

        result = main_mod.run(
            config, _NeverFindsDevice(), lock_path=tmp_path / "l.lock"
        )
        assert result == 0
        assert cleanup_calls == [None]

    def test_shutdown_while_open_keeps_failing_still_blanks_the_known_device(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Proves the fix's actual value over relying solely on

        `_run_active`'s own per-session cleanup: a device that was found
        but never made it into an active session (every `deck.open()`
        attempt failed) still gets passed to `_shutdown_cleanup` when
        shutdown fires -- because `current_deck` is set right after
        `_find_deck` succeeds, independent of whether `_run_active` was
        ever entered.
        """
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            hidhelp,
            "explain_open_failure",
            lambda error, **k: hidhelp.Guidance(
                status="warn", message="fake guidance", state="W7"
            ),
        )
        monkeypatch.setattr(
            statusfile, "default_status_path", lambda: tmp_path / "status.json"
        )
        config = _make_config(tmp_path)

        deck = _AlwaysFailsToOpenDeck()
        manager = _FakeManagerAlwaysFindsDevice(deck)

        shutting_down = threading.Event()
        call_count = {"n": 0}
        real_wait = shutting_down.wait

        def _wait(seconds: float) -> bool:
            call_count["n"] += 1
            if call_count["n"] >= 3:
                shutting_down.set()
            return real_wait(0)

        monkeypatch.setattr(shutting_down, "wait", _wait)
        monkeypatch.setattr(main_mod, "_install_signal_handler", lambda: shutting_down)

        cleanup_calls: list[Any] = []
        real_cleanup = main_mod._shutdown_cleanup

        def _spy(candidate: Any) -> None:
            cleanup_calls.append(candidate)
            real_cleanup(candidate)

        monkeypatch.setattr(main_mod, "_shutdown_cleanup", _spy)

        result = main_mod.run(config, manager, lock_path=tmp_path / "l.lock")
        assert result == 0
        # The last call is the one that matters (fires from the finally);
        # it must be the actual device, not None, even though open() never
        # once succeeded.
        assert cleanup_calls[-1] is deck
