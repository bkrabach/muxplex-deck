"""`muxplex-deck doctor` -- pure check helpers, mocked inputs, no hardware/server.

Each check helper is exercised in isolation with fakes/mocks (a fake
`subprocess.run` for openssl/systemctl/launchctl, a fake `DeviceManager` for
deck probing, a fake `httpx.Client` for the server-reachability check).
`doctor()` itself is proven to never raise and always return 0, including
when every single check fails/warns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import cli


# ---------------------------------------------------------------------------
# check_python_version
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_current_interpreter_is_ok(self) -> None:
        status, _message = cli.check_python_version()
        assert status == "ok"


# ---------------------------------------------------------------------------
# check_config_file
# ---------------------------------------------------------------------------


class TestCheckConfigFile:
    def test_missing_config_warns(self, tmp_path: Path) -> None:
        status, message = cli.check_config_file(str(tmp_path / "config.json"))
        assert status == "warn"
        assert "not yet created" in message

    def test_existing_config_is_ok(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text("{}", encoding="utf-8")
        status, message = cli.check_config_file(str(path))
        assert status == "ok"
        assert str(path) in message


# ---------------------------------------------------------------------------
# check_federation_key
# ---------------------------------------------------------------------------


class TestCheckFederationKey:
    def test_missing_key_file_warns(self, tmp_path: Path) -> None:
        status, message = cli.check_federation_key(tmp_path / "federation_key")
        assert status == "warn"
        assert "not found" in message

    def test_world_readable_key_warns_and_never_prints_key(
        self, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "federation_key"
        key_file.write_text("supersecret\n", encoding="utf-8")
        key_file.chmod(0o644)
        status, message = cli.check_federation_key(key_file)
        assert status == "warn"
        assert "chmod 600" in message
        assert "supersecret" not in message

    def test_mode_0600_is_ok_and_never_prints_key(self, tmp_path: Path) -> None:
        key_file = tmp_path / "federation_key"
        key_file.write_text("supersecret\n", encoding="utf-8")
        key_file.chmod(0o600)
        status, message = cli.check_federation_key(key_file)
        assert status == "ok"
        assert "supersecret" not in message


# ---------------------------------------------------------------------------
# check_ca_file -- the CA:FALSE (leaf-cert-instead-of-CA) gotcha
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCheckCaFile:
    def test_none_is_ok(self) -> None:
        status, message = cli.check_ca_file(None)
        assert status == "ok"
        assert "not configured" in message

    def test_missing_file_warns(self, tmp_path: Path) -> None:
        status, message = cli.check_ca_file(tmp_path / "missing.crt")
        assert status == "warn"
        assert "not found" in message

    def test_ca_true_is_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca_file = tmp_path / "ca.crt"
        ca_file.write_text("fake cert bytes", encoding="utf-8")
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                0, stdout="X509v3 Basic Constraints:\nCA:TRUE\n"
            ),
        )
        status, message = cli.check_ca_file(ca_file)
        assert status == "ok"
        assert "valid CA" in message

    def test_ca_false_warns_loudly_with_leaf_cert_hint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact real-world mistake: pointing ca_file at the server's LEAF cert."""
        ca_file = tmp_path / "muxplex.crt"
        ca_file.write_text("fake leaf cert bytes", encoding="utf-8")
        monkeypatch.setattr(
            cli.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(
                0, stdout="X509v3 Basic Constraints:\nCA:FALSE\n"
            ),
        )
        status, message = cli.check_ca_file(ca_file)
        assert status == "warn"
        assert "CA:FALSE" in message
        assert "LEAF" in message
        assert "unable to get local issuer certificate" in message

    def test_openssl_missing_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ca_file = tmp_path / "ca.crt"
        ca_file.write_text("x", encoding="utf-8")

        def _raise(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("no openssl")

        monkeypatch.setattr(cli.subprocess, "run", _raise)
        status, message = cli.check_ca_file(ca_file)
        assert status == "warn"
        assert "openssl not found" in message


# ---------------------------------------------------------------------------
# probe_deck_status / check_deck_detected / check_hid_openable
# ---------------------------------------------------------------------------


class _FakeDeck:
    def __init__(self, *, openable: bool = True) -> None:
        self._openable = openable
        self.closed = False

    def open(self) -> None:
        if not self._openable:
            raise PermissionError("Permission denied")

    def close(self) -> None:
        self.closed = True

    def deck_type(self) -> str:
        return "Stream Deck Original"

    def get_serial_number(self) -> str:
        return "FAKE1"

    def get_firmware_version(self) -> str:
        return "1.0"

    def vendor_id(self) -> int:
        return 0x0FD9

    def product_id(self) -> int:
        return 0x0060

    def key_count(self) -> int:
        return 15

    def key_layout(self) -> tuple[int, int]:
        return (3, 5)

    def key_image_format(self) -> dict:
        return {
            "size": (72, 72),
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    def dial_count(self) -> int:
        return 0

    def touch_key_count(self) -> int:
        return 0

    def is_touch(self) -> bool:
        return False

    def is_visual(self) -> bool:
        return True

    def touchscreen_image_format(self) -> dict:
        return {"size": (0, 0), "format": "JPEG", "flip": (False, False), "rotation": 0}


class _FakeManager:
    def __init__(self, deck: _FakeDeck | None) -> None:
        self._deck = deck

    def find_device(self) -> _FakeDeck | None:
        return self._deck


class TestProbeDeckStatus:
    def test_no_device_found(self) -> None:
        result = cli.probe_deck_status(_FakeManager(None))
        assert result == {
            "found": False,
            "openable": False,
            "caps": None,
            "error": None,
        }

    def test_device_found_and_openable(self) -> None:
        result = cli.probe_deck_status(_FakeManager(_FakeDeck(openable=True)))
        assert result["found"] is True
        assert result["openable"] is True
        assert result["caps"] is not None
        assert result["caps"]["model"] == "Stream Deck Original"

    def test_device_found_but_not_openable(self) -> None:
        result = cli.probe_deck_status(_FakeManager(_FakeDeck(openable=False)))
        assert result["found"] is True
        assert result["openable"] is False
        assert result["caps"] is None
        assert "Permission denied" in result["error"]

    def test_enumeration_error_is_captured(self) -> None:
        class _BrokenManager:
            def find_device(self):
                raise RuntimeError("usb bus error")

        result = cli.probe_deck_status(_BrokenManager())
        assert result["found"] is False
        assert "usb bus error" in result["error"]


class TestCheckDeckDetectedAndHidOpenable:
    def test_no_device_warns_with_guidance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _FakeManager(None)
        )
        status, message = cli.check_deck_detected()
        assert status == "warn"
        assert "No Stream Deck found" in message

    def test_device_detected_reports_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _FakeManager(_FakeDeck())
        )
        status, message = cli.check_deck_detected()
        assert status == "ok"
        assert "Stream Deck Original" in message
        assert "15 keys" in message

    def test_hid_not_openable_warns_with_udev_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=False)),
        )
        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(cli.sys, "platform", "linux")
        status, message = cli.check_hid_openable()
        assert status == "warn"
        assert "could not open device" in message

    def test_hid_openable_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=True)),
        )
        status, message = cli.check_hid_openable()
        assert status == "ok"


# ---------------------------------------------------------------------------
# check_server_reachable
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class _FakeHttpxClient:
    def __init__(self, response: _FakeResponse | Exception, **_kwargs: Any) -> None:
        self._response = response

    def __enter__(self) -> "_FakeHttpxClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class TestCheckServerReachable:
    def test_no_server_url_warns(self) -> None:
        status, message = cli.check_server_reachable("", None)
        assert status == "warn"
        assert "not configured" in message

    def test_reachable_server_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        response = _FakeResponse({"name": "spark-1", "version": "0.7.1"})
        monkeypatch.setattr(httpx, "Client", lambda **k: _FakeHttpxClient(response))
        status, message = cli.check_server_reachable("https://spark-1:8088", None)
        assert status == "ok"
        assert "spark-1" in message
        assert "0.7.1" in message

    def test_tls_error_hints_at_ca_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        exc = httpx.ConnectError(
            "certificate verify failed: unable to get local issuer certificate"
        )
        monkeypatch.setattr(httpx, "Client", lambda **k: _FakeHttpxClient(exc))
        status, message = cli.check_server_reachable("https://spark-1:8088", None)
        assert status == "warn"
        assert "TLS verification failed" in message

    def test_connection_error_is_generic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        exc = httpx.ConnectError("Connection refused")
        monkeypatch.setattr(httpx, "Client", lambda **k: _FakeHttpxClient(exc))
        status, message = cli.check_server_reachable("https://spark-1:8088", None)
        assert status == "warn"
        assert "unreachable" in message.lower()


# ---------------------------------------------------------------------------
# doctor() -- never raises, always returns 0, even when everything fails
# ---------------------------------------------------------------------------


class TestDoctorNeverRaises:
    def test_doctor_returns_zero_with_everything_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Point every path at a nonexistent tmp location and force every
        # external probe to fail/warn -- doctor() must still complete cleanly.
        monkeypatch.setattr(
            cli, "check_deck_detected", lambda config_path=None: ("warn", "no device")
        )
        monkeypatch.setattr(cli, "check_hid_openable", lambda: ("warn", "n/a"))
        monkeypatch.setattr(
            cli, "check_service_status", lambda: ("warn", "not installed")
        )
        monkeypatch.setattr(
            cli, "check_install_and_update", lambda: ("warn", "unknown install source")
        )
        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))
        assert result == 0

    def test_doctor_prints_header_and_footer(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            cli, "check_deck_detected", lambda config_path=None: ("warn", "no device")
        )
        monkeypatch.setattr(cli, "check_hid_openable", lambda: ("warn", "n/a"))
        monkeypatch.setattr(
            cli, "check_service_status", lambda: ("warn", "not installed")
        )
        monkeypatch.setattr(
            cli, "check_install_and_update", lambda: ("ok", "up to date")
        )
        cli.doctor(str(tmp_path / "nonexistent-config.json"))
        out = capsys.readouterr().out
        assert "muxplex-deck doctor" in out
