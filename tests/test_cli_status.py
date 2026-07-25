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
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "Service: running" in out
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
        assert "No status file found" in out
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
        monkeypatch.setattr(statusfile, "default_status_path", lambda: status_path)

        rc = cli.status()
        out = capsys.readouterr().out
        assert rc == 0
        assert "stale" not in out.lower()


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
        assert "Service: not running" in out
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
        assert "Service: not running" in out
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
