"""`muxplex-deck doctor` -- pure check helpers, mocked inputs, no hardware/server.

Each check helper is exercised in isolation with fakes/mocks (a fake
`subprocess.run` for openssl/systemctl/launchctl, a fake `DeviceManager` for
deck probing, a fake `httpx.Client` for the server-reachability check).
`doctor()` itself is proven to never raise and always return 0, including
when every single check fails/warns.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

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

    def test_windows_reports_presence_only_never_prints_chmod(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WINDOWS_NATIVE_SPEC.md section 3.3: `Path.chmod()` doesn't restrict

        other users on Windows, so `st_mode` reads back permissive
        regardless of what actually protects the file (NTFS ACLs) --
        printing a `chmod` remediation that cannot work violates this
        repo's own rule (AGENTS.md: never print a command that cannot work
        on the machine you are printing it to). Windows must report
        presence only, with `ok` status, and no `chmod` text.
        """
        key_file = tmp_path / "federation_key"
        key_file.write_text("supersecret\n", encoding="utf-8")
        monkeypatch.setattr(cli.sys, "platform", "win32")
        status, message = cli.check_federation_key(key_file)
        assert status == "ok"
        assert "chmod" not in message
        assert "NTFS" in message
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

    def test_openssl_missing_on_windows_is_ok_not_warn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows doesn't ship openssl by default -- unlike POSIX, where
        its absence is a warn-worthy anomaly, here it's expected. Flagging
        it as a "warn" implies the user broke something they need to fix;
        it's not their problem, so this must not use `warn`.
        """
        ca_file = tmp_path / "ca.crt"
        ca_file.write_text("x", encoding="utf-8")

        def _raise(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("no openssl")

        monkeypatch.setattr(cli.subprocess, "run", _raise)
        monkeypatch.setattr(cli.sys, "platform", "win32")
        status, message = cli.check_ca_file(ca_file)
        assert status == "ok"
        assert "windows" in message.lower()
        # Still actionable for anyone who wants deeper verification, just
        # not framed as something broken.
        assert "fingerprint" in message.lower()


# ---------------------------------------------------------------------------
# check_hidapi_dll -- WINDOWS_NATIVE_SPEC.md section 2.6's diagnosability
# requirement: whichever hidapi.dll streamdeck's loader will actually use
# must be visible in `doctor`, not just success/failure.
# ---------------------------------------------------------------------------


class TestCheckHidapiDll:
    def test_none_on_non_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        assert cli.check_hidapi_dll() is None

    def test_warns_when_vendored_dll_absent(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from muxplex_deck import hidapi_win

        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(hidapi_win, "ensure_hidapi", lambda: None)
        result = cli.check_hidapi_dll()
        assert result is not None
        status, message = result
        assert status == "warn"
        assert "github.com/libusb/hidapi" in message

    def test_warns_when_resolved_path_differs_from_vendored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from muxplex_deck import hidapi_win

        vendored = tmp_path / "x64" / "hidapi.dll"
        shadowing = str(tmp_path / "shadow" / "hidapi.dll")
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(hidapi_win, "ensure_hidapi", lambda: vendored.parent)
        monkeypatch.setattr(hidapi_win, "vendored_dll_path", lambda: vendored)
        monkeypatch.setattr(hidapi_win, "resolved_library_path", lambda: shadowing)
        result = cli.check_hidapi_dll()
        assert result is not None
        status, message = result
        assert status == "warn"
        assert shadowing in message
        assert "NOT the vendored copy" in message

    def test_ok_when_resolved_matches_vendored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from muxplex_deck import hidapi_win

        vendored = tmp_path / "x64" / "hidapi.dll"
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(hidapi_win, "ensure_hidapi", lambda: vendored.parent)
        monkeypatch.setattr(hidapi_win, "vendored_dll_path", lambda: vendored)
        monkeypatch.setattr(hidapi_win, "resolved_library_path", lambda: str(vendored))
        result = cli.check_hidapi_dll()
        assert result is not None
        status, message = result
        assert status == "ok"
        assert str(vendored) in message

    def test_warns_when_nothing_resolves_at_all(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from muxplex_deck import hidapi_win

        vendored = tmp_path / "x64" / "hidapi.dll"
        monkeypatch.setattr(cli.sys, "platform", "win32")
        monkeypatch.setattr(hidapi_win, "ensure_hidapi", lambda: vendored.parent)
        monkeypatch.setattr(hidapi_win, "vendored_dll_path", lambda: vendored)
        monkeypatch.setattr(hidapi_win, "resolved_library_path", lambda: None)
        result = cli.check_hidapi_dll()
        assert result is not None
        status, message = result
        assert status == "warn"
        assert "did not resolve" in message


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


# ---------------------------------------------------------------------------
# Regression: describe_capabilities() calls is_visual()/touch_key_count()/
# vendor_id()/product_id() -- RealDeckDevice didn't implement them, so any
# real Stream Deck+ crashed `muxplex-deck doctor` with an AttributeError.
# This exercises the exact probe_deck_status -> describe_capabilities path
# check_deck_detected uses, and pins that EVERY key the capability dict
# promises actually comes back populated -- not just "caps is not None".
# ---------------------------------------------------------------------------

_EXPECTED_CAPABILITY_KEYS = {
    "model",
    "serial",
    "firmware",
    "vendor_id",
    "product_id",
    "key_count",
    "key_rows",
    "key_cols",
    "key_image_size",
    "key_image_format",
    "dial_count",
    "touch_key_count",
    "has_touchscreen",
    "touchscreen_size",
    "is_visual",
}


class TestProbeDeckStatusFullCapabilityDict:
    def test_capability_dict_has_every_expected_key_populated(self) -> None:
        result = cli.probe_deck_status(_FakeManager(_FakeDeck(openable=True)))
        assert result["found"] is True
        assert result["openable"] is True
        caps = result["caps"]
        assert caps is not None
        assert set(caps.keys()) == _EXPECTED_CAPABILITY_KEYS
        # Spot-check the four fields that were the actual crash: a missing
        # method would raise AttributeError before this dict ever got
        # built, so simply reaching these assertions is the proof.
        assert caps["is_visual"] is True
        assert caps["touch_key_count"] == 0
        assert caps["vendor_id"] == 0x0FD9
        assert caps["product_id"] == 0x0060


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
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
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
        status, _message = cli.check_hid_openable()
        assert status == "ok"

    def test_no_device_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _FakeManager(None)
        )
        status, message = cli.check_hid_openable()
        assert status == "warn"
        assert "no Stream Deck detected" in message

    def test_open_fails_but_our_own_service_is_running_is_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exclusive HID access: once our service holds the device, a second

        open() attempt (this very check) is EXPECTED to fail -- that must be
        reported as ok, not a warning, and must be keyed off service state
        (not the error string, which is unverifiable across platforms).
        """
        import muxplex_deck.device_real as device_real_mod
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=False)),
        )
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)
        status, message = cli.check_hid_openable()
        assert status == "ok"
        assert "in use by the muxplex-deck service" in message
        assert "expected" in message

    def test_open_fails_and_service_not_running_is_unchanged_warn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without our own service holding it, a failed open is a genuine

        HID-permission problem -- behavior here must be byte-for-byte the
        pre-existing warn + udev hint, not silently downgraded.
        """
        import muxplex_deck.device_real as device_real_mod
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=False)),
        )
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(cli.sys, "platform", "linux")
        status, message = cli.check_hid_openable()
        assert status == "warn"
        assert "could not open device" in message
        assert "service install" in message


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

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
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
# check_federation_key_auth -- the actual root cause of a real regression:
# without `Accept: application/json`, muxplex's auth middleware answers an
# authenticated request with a 307 redirect to /login instead of 401/200.
# This client deliberately does not follow redirects (same reasoning as
# `muxplex_client.MuxplexClient`'s docstring -- following it would land on
# the login page and misreport a 200), so the redirect used to surface as
# "unexpected response HTTP 307" and read as "can't verify", letting a
# wrong key through unvalidated. The fix is the header, NOT
# `follow_redirects=True`.
# ---------------------------------------------------------------------------


class _FakeAuthResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _FakeAuthMiddlewareClient:
    """Simulates the real server behavior that caused the regression: a
    request without `Accept: application/json` gets 307-redirected to
    /login instead of answered with 401/200.
    """

    def __init__(self, accepted_key: str, **_kwargs: Any) -> None:
        self._accepted_key = accepted_key

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeAuthResponse:
        headers = headers or {}
        if headers.get("Accept") != "application/json":
            return _FakeAuthResponse(307)
        token = headers.get("Authorization", "")
        if token == f"Bearer {self._accepted_key}":
            return _FakeAuthResponse(200)
        return _FakeAuthResponse(401)


class TestCheckFederationKeyAuth:
    def test_correct_key_is_verified_not_unverifiable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression pin: this used to come back HTTP 307 and read as
        'could not verify' -- with the Accept header sent, the real
        auth middleware now answers definitively.
        """
        import httpx

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **k: _FakeAuthMiddlewareClient("right-key", **k),
        )
        status, message = cli.check_federation_key_auth(
            "https://spark-1:8088", "right-key"
        )
        assert status == "ok"
        assert "accepted" in message.lower()

    def test_wrong_key_is_rejected_not_unverifiable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **k: _FakeAuthMiddlewareClient("right-key", **k),
        )
        status, message = cli.check_federation_key_auth(
            "https://spark-1:8088", "wrong-key"
        )
        assert status == "fail"
        assert "rejected" in message.lower()

    def test_request_actually_sends_accept_json_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the mechanism, not just the outcome: assert the real
        request this function builds includes the header the server's
        auth layer branches on, so this regression cannot silently
        return via some future refactor that drops it.
        """
        import httpx

        captured: dict[str, str] = {}

        class _CapturingClient:
            def __init__(self, **_k: Any) -> None:
                pass

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def get(
                self, url: str, headers: dict[str, str] | None = None
            ) -> _FakeAuthResponse:
                captured.update(headers or {})
                return _FakeAuthResponse(200)

        monkeypatch.setattr(httpx, "Client", lambda **k: _CapturingClient(**k))
        cli.check_federation_key_auth("https://spark-1:8088", "some-key")
        assert captured.get("Accept") == "application/json"
        assert captured.get("Authorization") == "Bearer some-key"

    def test_unexpected_status_still_warns_without_fabricating_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely unexpected status code (not a redirect this fix
        eliminates, but e.g. a 500) must still degrade to warn -- this
        function must never claim a definitive answer it doesn't have.
        """
        import httpx

        monkeypatch.setattr(
            httpx, "Client", lambda **k: _FakeHttpxClient(_FakeResponse({}))
        )

        class _FiveHundredClient:
            def __init__(self, **_k: Any) -> None:
                pass

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *_exc: object) -> None:
                return None

            def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
                return _FakeAuthResponse(500)

        monkeypatch.setattr(httpx, "Client", lambda **k: _FiveHundredClient(**k))
        status, message = cli.check_federation_key_auth(
            "https://spark-1:8088", "some-key"
        )
        assert status == "warn"
        assert "500" in message


# ---------------------------------------------------------------------------
# _check_for_update -- pypi source
#
# muxplex-deck 0.4.0+ is published to PyPI; this is the known-source path
# doctor previously lacked (it fell through to "unknown install source").
# Network is mocked via `httpx.get` (the module calls it directly, not via
# `httpx.Client(...)` like check_server_reachable above).
# ---------------------------------------------------------------------------


class _FakePyPIResponse:
    def __init__(
        self, version: str | None = None, raise_exc: Exception | None = None
    ) -> None:
        self._version = version
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self) -> dict:
        return {"info": {"version": self._version}}


class TestCheckForUpdatePypi:
    @staticmethod
    def _info(version: str = "0.4.0") -> dict:
        return {"source": "pypi", "version": version, "commit": None, "url": None}

    def test_up_to_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakePyPIResponse("0.4.0"))

        update_available, message = cli._check_for_update(self._info("0.4.0"))

        assert update_available is False
        assert "up to date" in message
        assert "0.4.0" in message

    def test_update_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakePyPIResponse("0.5.0"))

        update_available, message = cli._check_for_update(self._info("0.4.0"))

        assert update_available is True
        assert "0.4.0" in message
        assert "0.5.0" in message

    def test_network_failure_degrades_to_upgrade_to_be_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        def _raise(*_a: object, **_k: object) -> None:
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "get", _raise)

        update_available, message = cli._check_for_update(self._info("0.4.0"))

        assert update_available is True
        assert "could not check PyPI" in message

    def test_bad_response_degrades_gracefully(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *a, **k: _FakePyPIResponse(raise_exc=ValueError("404")),
        )

        update_available, message = cli._check_for_update(self._info("0.4.0"))

        assert update_available is True
        assert "could not check PyPI" in message


# ---------------------------------------------------------------------------
# check_service_status -- three distinct states, not two. A real user's
# `service install` crash-looped 1113 times (no config yet), and the very
# next `doctor` line said "not installed -- run: muxplex-deck service
# install" about a service that WAS installed and actively failing --
# conflating "not active" with "not installed" and recommending an action
# that was already done. See AGENTS.md for the incident.
# ---------------------------------------------------------------------------


class TestCheckServiceStatus:
    def test_not_installed_recommends_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl")
        monkeypatch.setattr(service_mod, "service_is_installed", lambda: False)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)

        status, message = cli.check_service_status()

        assert status == "warn"
        assert "not installed" in message
        assert "muxplex-deck service install" in message

    def test_installed_but_not_active_does_not_recommend_install(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pins the exact bug: an installed, crash-looping service must NOT

        be told to run `service install` again -- it's already installed.
        """
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl")
        monkeypatch.setattr(service_mod, "service_is_installed", lambda: True)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)

        status, message = cli.check_service_status()

        assert status == "warn"
        assert "installed" in message
        assert "not running" in message
        assert "service install" not in message
        assert "service logs" in message

    def test_installed_and_active_is_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/systemctl")
        monkeypatch.setattr(service_mod, "service_is_installed", lambda: True)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        status, message = cli.check_service_status()

        assert status == "ok"
        assert "running" in message

    def test_missing_tool_reports_clearly_not_as_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)

        status, message = cli.check_service_status()

        assert status == "warn"
        assert "systemctl not found" in message

    def test_launchd_installed_and_active_is_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.service as service_mod

        monkeypatch.setattr(cli.sys, "platform", "darwin")
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/launchctl")
        monkeypatch.setattr(service_mod, "service_is_installed", lambda: True)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        status, message = cli.check_service_status()

        assert status == "ok"
        assert "launchd" in message
        assert "running" in message

    def test_windows_reports_not_yet_supported_not_missing_systemctl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On native Windows, `systemctl` will never be found -- that's not
        a missing tool the user needs to install, it's a not-yet-built
        platform increment (Task Scheduler support). The message must say
        that, not frame it as a missing Linux binary.
        """
        monkeypatch.setattr(cli.sys, "platform", "win32")
        # Even if something named "systemctl" happened to resolve on
        # PATH, win32 must short-circuit before ever consulting `which`.
        monkeypatch.setattr(
            cli.shutil, "which", lambda name: (_ for _ in ()).throw(AssertionError)
        )

        status, message = cli.check_service_status()

        assert status == "warn"
        assert "isn't supported on windows yet" in message.lower()
        assert "systemctl" not in message.lower()
        assert "muxplex-deck" in message


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


# ---------------------------------------------------------------------------
# doctor() end-to-end through the REAL check_deck_detected/check_hid_openable
# (unlike TestDoctorNeverRaises above, which stubs those two out) -- this is
# the path a user with hardware attached actually takes, and the one that
# crashed with AttributeError before the RealDeckDevice fix.
# ---------------------------------------------------------------------------


class TestDoctorDeckIntegrationEndToEnd:
    def test_device_present_does_not_raise_and_reports_found_and_openable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=True)),
        )
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        # The real assertion: doctor() must not raise. Before the fix this
        # AttributeError'd inside check_deck_detected -> probe_deck_status ->
        # describe_capabilities(RealDeckDevice-shaped deck).is_visual().
        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        assert "Stream Deck Original" in out
        assert "15 keys" in out
        assert "HID: device opened successfully" in out

    def test_no_device_present_still_reports_not_found_guidance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression guard: the fix must not disturb the no-hardware path."""
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _FakeManager(None)
        )
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        assert "No Stream Deck found" in out


# ---------------------------------------------------------------------------
# doctor()'s new environment-guidance section (WSL_COLD_START_SPEC.md).
# W0 (not WSL) + udev live must add NOTHING -- this is the no-regression
# guard for macOS/healthy-Linux users' doctor output.
# ---------------------------------------------------------------------------


class TestDoctorEnvironmentGuidanceNoRegression:
    def test_no_wsl_and_udev_live_adds_no_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The critical no-noise case (success criterion #9 / #5)."""
        from muxplex_deck import hidhelp

        monkeypatch.setattr(hidhelp, "explain_environment", lambda **_kw: [])
        monkeypatch.setattr(
            cli, "check_deck_detected", lambda config_path=None: ("warn", "no device")
        )
        monkeypatch.setattr(cli, "check_hid_openable", lambda: ("warn", "n/a"))
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        # Only the pre-existing checks appear -- nothing from the new
        # environment section, which returned [].
        assert "no device" in out

    def test_environment_guidance_is_surfaced_before_device_checks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            hidhelp,
            "explain_environment",
            lambda **_kw: [
                hidhelp.Guidance(status="warn", message="WSL guidance line", state="W4")
            ],
        )
        monkeypatch.setattr(
            cli, "check_deck_detected", lambda config_path=None: ("warn", "no device")
        )
        monkeypatch.setattr(cli, "check_hid_openable", lambda: ("warn", "n/a"))
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        assert "WSL guidance line" in out
        # Environment guidance appears before the "no device" line.
        assert out.index("WSL guidance line") < out.index("no device")


# ---------------------------------------------------------------------------
# check_hid_openable()'s hint text now delegates to hidhelp -- must still
# preserve the exact pre-existing behavior for native-Linux callers.
# ---------------------------------------------------------------------------


class TestCheckHidOpenableHintDelegatesToHidhelp:
    def test_hint_uses_hidhelp_run_service_install_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod
        import muxplex_deck.service as service_mod
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=False)),
        )
        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(cli.sys, "platform", "linux")

        status, message = cli.check_hid_openable()

        assert status == "warn"
        assert hidhelp.HID_HINT_RUN_SERVICE_INSTALL in message

    def test_hint_uses_hidhelp_rule_exists_but_failed_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod
        import muxplex_deck.service as service_mod
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=False)),
        )
        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: True)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(cli.sys, "platform", "linux")

        status, message = cli.check_hid_openable()

        assert status == "warn"
        assert hidhelp.HID_HINT_RULE_EXISTS_BUT_STILL_FAILED in message


# ---------------------------------------------------------------------------
# _NO_DEVICE_GUIDANCE (V10 fix) -- must no longer leak WSL/udev text on
# every platform. This is a deliberate content change (not "byte-identical"
# for this specific string) -- see WSL_COLD_START_SPEC.md section 7.7.
# ---------------------------------------------------------------------------


class TestNoDeviceGuidanceNoLongerLeaksWslText:
    def test_no_wsl_or_udev_mentions(self) -> None:
        text = cli._NO_DEVICE_GUIDANCE
        assert "usbipd" not in text.lower()
        assert "udev" not in text.lower()
        assert "wsl" not in text.lower()

    def test_still_mentions_elgato_app_and_cable(self) -> None:
        text = cli._NO_DEVICE_GUIDANCE
        assert "Elgato Stream Deck app" in text
        assert "cable" in text.lower()


# ---------------------------------------------------------------------------
# check_config_file -- must point at the CLI's own `init` command, not the
# README (doctor is supposed to teach; docs merely supplement -- see AGENTS.md
# "single home for guidance" convention applied to config, not just HID/WSL).
# ---------------------------------------------------------------------------


class TestCheckConfigFilePointsAtInitCommand:
    def test_missing_config_points_at_init_not_readme(self, tmp_path: Path) -> None:
        status, message = cli.check_config_file(str(tmp_path / "config.json"))
        assert status == "warn"
        assert "muxplex-deck init" in message
        assert "README" not in message


# ---------------------------------------------------------------------------
# check_hid_openable() must not offer the udev-rule hints on WSL -- they
# don't apply there (hidhelp.udev_guidance() returns None for WSL; the
# proven remediation is the per-attach chown from explain_environment(),
# already shown earlier in doctor()'s output).
# ---------------------------------------------------------------------------


class TestCheckHidOpenableSuppressesHintOnWsl:
    def test_wsl_open_failure_has_no_udev_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import muxplex_deck.device_real as device_real_mod
        import muxplex_deck.service as service_mod
        from muxplex_deck import wsl

        monkeypatch.setattr(
            device_real_mod,
            "RealDeviceManager",
            lambda: _FakeManager(_FakeDeck(openable=False)),
        )
        monkeypatch.setattr(service_mod, "udev_rule_exists", lambda: False)
        monkeypatch.setattr(service_mod, "service_is_active", lambda: False)
        monkeypatch.setattr(cli.sys, "platform", "linux")
        monkeypatch.setattr(
            wsl, "detect", lambda **_k: wsl.WslInfo(is_wsl=True, version=2, kernel="x")
        )

        status, message = cli.check_hid_openable()

        assert status == "warn"
        assert "could not open device" in message
        assert "service install" not in message
        assert "udev rule exists" not in message


# ---------------------------------------------------------------------------
# doctor()'s W7 collapse -- when explain_environment() already reports
# "attached, can't open it" (W7) plus the proven chown fix,
# check_deck_detected()/check_hid_openable() must not restate the same fact
# across two more (partly contradictory) lines. See the WSL cold-start bug
# report: "3 lines describe one problem".
# ---------------------------------------------------------------------------


class TestDoctorW7Collapse:
    def test_w7_guidance_suppresses_deck_detected_and_hid_openable_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            hidhelp,
            "explain_environment",
            lambda **_kw: [
                hidhelp.Guidance(
                    status="warn", message="attached, can't open it", state="W7"
                )
            ],
        )
        deck_detected_calls: list[object] = []
        hid_openable_calls: list[object] = []
        monkeypatch.setattr(
            cli,
            "check_deck_detected",
            lambda config_path=None: (
                deck_detected_calls.append(1)
                or (
                    "ok",
                    "Stream Deck detected (not yet opened -- see HID check below)",
                )
            ),
        )
        monkeypatch.setattr(
            cli,
            "check_hid_openable",
            lambda: hid_openable_calls.append(1) or ("warn", "HID: could not open"),
        )
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        assert deck_detected_calls == []
        assert hid_openable_calls == []
        out = capsys.readouterr().out
        assert "attached, can't open it" in out
        assert "see HID check below" not in out
        assert "HID: could not open" not in out
        # Collapsed to exactly one occurrence of the underlying fact.
        assert out.count("can't open it") == 1

    def test_non_w7_state_still_calls_both_checks_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression guard: only W7 collapses -- every other state (or no

        state at all) must keep calling both checks exactly as before.
        """
        from muxplex_deck import hidhelp

        monkeypatch.setattr(hidhelp, "explain_environment", lambda **_kw: [])
        monkeypatch.setattr(
            cli, "check_deck_detected", lambda config_path=None: ("warn", "no device")
        )
        monkeypatch.setattr(cli, "check_hid_openable", lambda: ("warn", "n/a"))
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        assert "no device" in out


# ---------------------------------------------------------------------------
# doctor()'s W1-W6 contradiction fix -- when a WSL state already located the
# device (or explains precisely why it isn't visible yet),
# check_deck_detected()'s generic "not found, check the cable" text must not
# also be shown -- it flatly contradicts the WSL guidance just above it. See
# the bug report: "plugged into Windows (BUSID 1-4) but not shared"
# immediately followed by "No Stream Deck found ... check the USB cable".
# ---------------------------------------------------------------------------


class TestDoctorW1ToW6CableCheckContradiction:
    def test_w4_state_replaces_generic_cable_check_text(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        from muxplex_deck import hidhelp

        monkeypatch.setattr(
            hidhelp,
            "explain_environment",
            lambda **_kw: [
                hidhelp.Guidance(
                    status="warn",
                    message="plugged into Windows (BUSID 1-4) but not shared",
                    state="W4",
                )
            ],
        )
        # The REAL check_deck_detected() function is used (not mocked) so it
        # returns the actual _NO_DEVICE_GUIDANCE constant, exercising the
        # exact string-equality swap doctor() performs.
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _FakeManager(None)
        )
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        assert "but not shared" in out
        # The contradictory generic guidance must be gone...
        assert "Check the USB cable and try a different port." not in out
        # ...replaced by a pointer back to the guidance already shown.
        assert "see the WSL guidance above" in out

    def test_no_env_guidance_keeps_generic_cable_check_unchanged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Regression guard: the no-hardware, non-WSL path (already covered

        by TestDoctorDeckIntegrationEndToEnd above) must be untouched.
        """
        from muxplex_deck import hidhelp

        monkeypatch.setattr(hidhelp, "explain_environment", lambda **_kw: [])
        import muxplex_deck.device_real as device_real_mod

        monkeypatch.setattr(
            device_real_mod, "RealDeviceManager", lambda: _FakeManager(None)
        )
        monkeypatch.setattr(cli, "check_service_status", lambda: ("ok", "n/a"))
        monkeypatch.setattr(cli, "check_install_and_update", lambda: ("ok", "n/a"))
        monkeypatch.setattr(
            cli, "check_server_reachable", lambda *a, **k: ("warn", "n/a")
        )

        result = cli.doctor(str(tmp_path / "nonexistent-config.json"))

        assert result == 0
        out = capsys.readouterr().out
        assert "No Stream Deck found" in out
