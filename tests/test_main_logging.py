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
