"""`muxplex-deck status` -- reads the sidecar's published status file rather

than probing the (possibly exclusively-held) device directly. All device/
service interactions are monkeypatched -- no hardware, no real service
manager, no real `~/.local/state` path (always a `tmp_path` fixture).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import cli, statusfile

_SAMPLE_CAPS: dict[str, Any] = {
    "model": "Stream Deck +",
    "serial": "AB12",
    "firmware": "1.2.3",
    "key_count": 8,
    "key_rows": 2,
    "key_cols": 4,
    "dial_count": 4,
    "has_touchscreen": True,
    "is_visual": True,
}


def _write_status(path: Path, **overrides: Any) -> dict[str, Any]:
    import time

    status = statusfile.build_status(
        pid=4242,
        device_connected=True,
        device_caps=_SAMPLE_CAPS,
        server_url="https://example:8088",
        server_connected=True,
        last_poll_at=time.time(),
        last_error=None,
        active_session="work",
        active_view="all",
        page=1,
    )
    status.update(overrides)
    statusfile.write_status(status, path)
    return status


class TestStatusServiceRunning:
    def test_reads_good_status_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        status_path = tmp_path / "status.json"
        _write_status(status_path)

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        # Matches the pid _write_status() records -- this IS the currently
        # running process's own status, so it should be trusted as current.
        monkeypatch.setattr(service_mod, "service_main_pid", lambda: 4242)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "Running." in out
        assert "Stream Deck +" in out
        assert "8 keys" in out
        assert "reachable" in out
        assert "work" in out

    def test_missing_status_file_gives_clear_guidance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        missing_path = tmp_path / "status.json"  # never written

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: missing_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "no status file yet" in out
        assert "service logs" in out

    def test_stale_timestamp_is_flagged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        status_path = tmp_path / "status.json"
        stale_status = _write_status(status_path)
        stale_status["updated_at"] = 0.0  # 1970 -- guaranteed stale
        statusfile.write_status(stale_status, status_path)

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        # Pid still matches the live process -- this is genuinely the SAME
        # process just not writing (stuck), not a stale previous incarnation.
        monkeypatch.setattr(service_mod, "service_main_pid", lambda: 4242)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "stale" in out.lower()

    def test_fresh_timestamp_is_not_flagged_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        status_path = tmp_path / "status.json"
        _write_status(status_path)

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(service_mod, "service_main_pid", lambda: 4242)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "stale" not in out.lower()


class TestStatusPidFreshnessGuard:
    """Guards the bug CLASS this session found repeatedly: state reported

    that wasn't actually observed this instant. Age alone can't tell a
    dying-but-recently-written process apart from the one running right
    now (the `service restart` race in AGENTS.md: 2 false failures read
    from a previous incarnation's stale-but-recent status write). Only a
    live pid comparison can.
    """

    def test_pid_mismatch_reports_unknown_not_failed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        status_path = tmp_path / "status.json"
        # Written by a PREVIOUS incarnation (pid 1111): device disconnected,
        # server unreachable -- the exact stale snapshot a dying process
        # leaves behind. Age is small (just written), so an age-only check
        # would call this "fresh" and report it as current truth.
        _write_status(
            status_path,
            pid=1111,
            device={"connected": False},
            server={
                "url": "https://example:8088",
                "connected": False,
                "last_error": "Authentication required",
            },
        )

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        # The service manager says the CURRENT process is pid 2222 --
        # different from what's recorded in status.json.
        monkeypatch.setattr(service_mod, "service_main_pid", lambda: 2222)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        # Must NOT present the previous incarnation's failures as current --
        # this is the false alarm the restart race produced verbatim.
        assert "Device: not connected" not in out
        assert "unreachable" not in out
        assert "Authentication required" not in out
        # Must say "unknown", not silently show ok/warn from stale data.
        assert "previous run" in out.lower() or "not yet available" in out.lower()

    def test_pid_undetermined_falls_back_to_age_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """When the live pid can't be determined at all (unsupported

        platform, service manager command failure), fall back to the
        original age-based staleness check rather than refusing to report
        anything.
        """
        status_path = tmp_path / "status.json"
        _write_status(status_path)

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(service_mod, "service_main_pid", lambda: None)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        # Fresh data, pid undeterminable -- still reports the real device/
        # server state (age-based fallback), not a blanket "unknown".
        assert "Stream Deck +" in out
        assert "reachable" in out


class TestStatusServiceNotRunning:
    def test_falls_back_to_direct_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Nothing holds the device while the service is stopped -- safe to

        probe it directly, and `status` should still be useful pre-install
        (before any status file has ever been written).
        """
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(
            statusfile, "default_status_path", lambda: tmp_path / "status.json"
        )

        probe_calls: list[str] = []

        def _fake_check_deck_detected(
            config_path: str | None = None,
        ) -> tuple[str, str]:
            probe_calls.append("called")
            return "ok", "Stream Deck Original: 15 keys (3x5)"

        monkeypatch.setattr(cli, "check_deck_detected", _fake_check_deck_detected)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert probe_calls == ["called"]
        assert "not running" in out
        assert "Stream Deck Original" in out

    def test_ignores_stale_status_file_when_not_running(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        status_path = tmp_path / "status.json"
        _write_status(status_path)

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)
        monkeypatch.setattr(
            cli,
            "check_deck_detected",
            lambda config_path=None: ("warn", "No Stream Deck found"),
        )

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "not running" in out
        assert "No Stream Deck found" in out


class TestStatusJson:
    def test_json_output_is_valid_and_parseable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        status_path = tmp_path / "status.json"
        _write_status(status_path)

        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status(as_json=True)
        out = capsys.readouterr().out
        assert rc == 0
        parsed = json.loads(out)
        assert parsed["service_running"] is True
        assert parsed["status"]["device"]["connected"] is True
        assert parsed["status"]["state"]["active_session"] == "work"

    def test_json_output_missing_file_is_still_valid_json(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(
            statusfile, "default_status_path", lambda: tmp_path / "missing.json"
        )

        rc = cli.status(as_json=True)
        out = capsys.readouterr().out
        assert rc == 1
        parsed = json.loads(out)
        assert parsed["status"] is None
