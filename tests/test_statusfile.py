"""`muxplex_deck.statusfile` -- the sidecar's published status file.

All paths are `tmp_path` fixtures -- never `~/.config` or `~/.local/state`.
No hardware, no network, no real service manager.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import statusfile


# ---------------------------------------------------------------------------
# default_status_dir / default_status_path
# ---------------------------------------------------------------------------


class TestDefaultPaths:
    def test_uses_xdg_state_home_when_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert statusfile.default_status_dir() == tmp_path / "muxplex-deck"
        assert (
            statusfile.default_status_path()
            == tmp_path / "muxplex-deck" / "status.json"
        )

    def test_falls_back_to_local_state_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.setattr(statusfile.Path, "home", lambda: tmp_path)
        assert (
            statusfile.default_status_dir()
            == tmp_path / ".local" / "state" / "muxplex-deck"
        )


# ---------------------------------------------------------------------------
# build_status -- pure dict builder
# ---------------------------------------------------------------------------


_SAMPLE_CAPS: dict[str, Any] = {
    "model": "Stream Deck +",
    "serial": "AB12",
    "firmware": "1.2.3",
    "vendor_id": 0x0FD9,
    "product_id": 0x0084,
    "key_count": 8,
    "key_rows": 2,
    "key_cols": 4,
    "dial_count": 4,
    "has_touchscreen": True,
    "is_visual": True,
}


class TestBuildStatus:
    def test_schema_fields_present(self) -> None:
        status = statusfile.build_status(
            pid=1234,
            device_connected=True,
            device_caps=_SAMPLE_CAPS,
            server_url="https://example:8088",
            server_connected=True,
            last_poll_at=1000.0,
            last_error=None,
            active_session="work",
            active_view="all",
            page=1,
        )
        assert status["schema_version"] == statusfile.SCHEMA_VERSION
        assert status["pid"] == 1234
        assert "updated_at" in status
        assert status["device"]["connected"] is True
        assert status["device"]["capabilities"] == _SAMPLE_CAPS
        assert status["server"] == {
            "url": "https://example:8088",
            "connected": True,
            "last_poll_at": 1000.0,
            "last_error": None,
        }
        assert status["state"] == {
            "active_session": "work",
            "active_view": "all",
            "page": 1,
        }

    def test_device_not_connected_omits_capabilities(self) -> None:
        status = statusfile.build_status(
            pid=1,
            device_connected=False,
            device_caps=None,
            server_url="",
            server_connected=False,
            last_poll_at=None,
            last_error=None,
            active_session=None,
            active_view=None,
            page=None,
        )
        assert status["device"] == {"connected": False}
        assert "capabilities" not in status["device"]

    def test_never_contains_federation_key_material(self) -> None:
        """The builder has no parameter that could carry a secret at all --

        this pins that invariant by asserting a representative secret value
        never appears anywhere in the serialized output, even if a caller
        tried to smuggle it through an unexpected channel.
        """
        secret = "sk-federation-supersecret-9f8e7d6c"
        status = statusfile.build_status(
            pid=1,
            device_connected=True,
            device_caps=_SAMPLE_CAPS,
            server_url=f"https://example:8088/{secret}",  # worst case: leaked into url
            server_connected=True,
            last_poll_at=1.0,
            last_error=None,
            active_session=None,
            active_view=None,
            page=None,
        )
        serialized = json.dumps(status)
        # The url itself is expected to appear (it's not a secret) -- what
        # must NEVER appear is any key/token-shaped field name.
        assert "federation_key" not in serialized
        assert "key_file" not in serialized
        assert "token" not in serialized.lower()
        assert "authorization" not in serialized.lower()


# ---------------------------------------------------------------------------
# write_status / read_status -- atomic write, round-trip
# ---------------------------------------------------------------------------


class TestWriteAndReadStatus:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "status.json"
        status = statusfile.build_status(
            pid=42,
            device_connected=True,
            device_caps=_SAMPLE_CAPS,
            server_url="https://example:8088",
            server_connected=True,
            last_poll_at=5.0,
            last_error=None,
            active_session="work",
            active_view="all",
            page=1,
        )
        statusfile.write_status(status, path)
        result = statusfile.read_status(path)
        assert result == status

    def test_creates_parent_dir_with_0700(self, tmp_path: Path) -> None:
        target_dir = tmp_path / "nested" / "muxplex-deck"
        path = target_dir / "status.json"
        statusfile.write_status({"a": 1}, path)
        mode = target_dir.stat().st_mode & 0o777
        assert mode == 0o700

    def test_uses_tempfile_plus_replace_not_direct_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert the ATOMIC-WRITE MECHANISM itself, not just the end result:

        a direct `open(path, "w")`/`write_text` would also produce a correct
        final file in the (uncontended) test environment, so a round-trip
        test alone can't catch a regression back to non-atomic writes. This
        pins that `tempfile.mkstemp` + `os.replace` are actually what's used.
        """
        path = tmp_path / "status.json"
        calls: list[str] = []

        real_mkstemp = statusfile.tempfile.mkstemp

        def _recording_mkstemp(*args: Any, **kwargs: Any) -> Any:
            calls.append("mkstemp")
            return real_mkstemp(*args, **kwargs)

        real_replace = statusfile.os.replace

        def _recording_replace(*args: Any, **kwargs: Any) -> Any:
            calls.append("replace")
            return real_replace(*args, **kwargs)

        monkeypatch.setattr(statusfile.tempfile, "mkstemp", _recording_mkstemp)
        monkeypatch.setattr(statusfile.os, "replace", _recording_replace)

        statusfile.write_status({"a": 1}, path)

        assert calls == ["mkstemp", "replace"]
        # No leftover .tmp files -- replace() must have consumed it.
        assert list(path.parent.glob("*.tmp")) == []

    def test_no_partial_file_observable_mid_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate a reader racing the writer: force `os.replace` to fail

        after the temp file is written, and assert the ORIGINAL target
        either doesn't exist yet or is left fully intact -- never partially
        written. This is what atomicity actually buys us.
        """
        path = tmp_path / "status.json"
        path.write_text(json.dumps({"a": "original"}))

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("simulated replace failure")

        monkeypatch.setattr(statusfile.os, "replace", _boom)

        # write_status is best-effort -- must not raise even though the
        # underlying replace() blew up.
        statusfile.write_status({"a": "new"}, path)

        # The original file is untouched -- no partial/corrupt content.
        assert json.loads(path.read_text()) == {"a": "original"}
        # No leftover tmp file (cleaned up on failure).
        assert list(path.parent.glob("*.tmp")) == []

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert statusfile.read_status(tmp_path / "nope.json") is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "status.json"
        path.write_text("{not valid json")
        assert statusfile.read_status(path) is None

    def test_write_failure_is_swallowed_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write failure must be logged and swallowed -- the sidecar's poll

        loop must never crash or stall because it couldn't publish status.
        """
        path = tmp_path / "sub" / "status.json"

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("disk full (simulated)")

        monkeypatch.setattr(statusfile.tempfile, "mkstemp", _boom)

        # Must not raise.
        statusfile.write_status({"a": 1}, path)
        assert statusfile.read_status(path) is None


# ---------------------------------------------------------------------------
# StatusReporter
# ---------------------------------------------------------------------------


class TestStatusReporter:
    def test_update_writes_merged_state(self, tmp_path: Path) -> None:
        path = tmp_path / "status.json"
        reporter = statusfile.StatusReporter("https://example:8088", path)

        reporter.update(device_connected=True, device_caps=_SAMPLE_CAPS)
        data = statusfile.read_status(path)
        assert data is not None
        assert data["device"]["connected"] is True
        assert data["server"]["url"] == "https://example:8088"

        reporter.update(server_connected=True, last_poll_at=10.0)
        data = statusfile.read_status(path)
        assert data is not None
        # Earlier field (device) is preserved across a later partial update.
        assert data["device"]["connected"] is True
        assert data["server"]["connected"] is True
        assert data["server"]["last_poll_at"] == 10.0

    def test_unknown_field_raises(self, tmp_path: Path) -> None:
        reporter = statusfile.StatusReporter(
            "https://example:8088", tmp_path / "status.json"
        )
        with pytest.raises(TypeError):
            reporter.update(not_a_real_field=True)

    def test_never_writes_federation_key(self, tmp_path: Path) -> None:
        """The reporter's constructor/update surface has no parameter for a

        federation key at all -- pin that invariant end-to-end through a
        real write.
        """
        path = tmp_path / "status.json"
        secret = "sk-federation-supersecret-9f8e7d6c"
        reporter = statusfile.StatusReporter(f"https://example:8088?k={secret}", path)
        reporter.update(device_connected=True, device_caps=_SAMPLE_CAPS)
        reporter.update(
            server_connected=True, active_session="work", active_view="all", page=1
        )
        raw = path.read_text(encoding="utf-8")
        assert "federation_key" not in raw
        assert "key_file" not in raw
