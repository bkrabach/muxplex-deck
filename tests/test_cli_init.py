"""`muxplex-deck init` -- the turnkey setup wizard, tmp fixtures + monkeypatch only.

SAFETY: every test isolates `$HOME` to a `tmp_path` subdirectory (so `~`
expansion for the CA cert / federation key default paths can never touch a
real `~/.config/muxplex-deck` or `~/.config/muxplex`), points `config.json`
at an explicit tmp path, fakes every `httpx.Client` call (no network), fakes
`subprocess.run` for the CA-vs-leaf openssl check, and stubs out
`service_install()` as a defense-in-depth belt-and-suspenders guard (the
wizard should never reach it in these tests since the "run service install?"
prompt always answers "n", but a real system service must never be touched
by a test run regardless).
"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from muxplex_deck import cli as cli_mod
from muxplex_deck import config as config_mod
from muxplex_deck import init_wizard
from muxplex_deck.config import DEFAULT_CONFIG

# ---------------------------------------------------------------------------
# Fakes: httpx.Client, subprocess.run (openssl)
# ---------------------------------------------------------------------------


class _FakeJsonResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._data


class _FakeCaResponse:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpxClient:
    """Routes GET requests by URL suffix to a scripted response/exception."""

    def __init__(self, responses: dict[str, Any], **_kwargs: Any) -> None:
        self._responses = responses

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
        for suffix, resp in self._responses.items():
            if url.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                if callable(resp):
                    return resp(headers)
                return resp
        raise AssertionError(f"unexpected URL requested in test: {url}")


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> None:
    monkeypatch.setattr(httpx, "Client", lambda **k: _FakeHttpxClient(responses, **k))


def _make_input(values: list[str]):
    it = iter(values)

    def _input(prompt: str) -> str:
        try:
            return next(it)
        except StopIteration as exc:
            raise AssertionError(
                f"input() called with no scripted values left (prompt: {prompt!r})"
            ) from exc

    return _input


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def deck_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate ALL `~`-relative paths (CA cert, federation key defaults) to a
    tmp directory -- Path.expanduser() honors $HOME on POSIX. Never touches
    the real ~/.config/muxplex-deck or ~/.config/muxplex.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SUDO_USER", raising=False)
    return home


@pytest.fixture
def config_path(tmp_path: Path) -> str:
    return str(tmp_path / "config.json")


@pytest.fixture(autouse=True)
def _never_touch_real_hardware_or_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense in depth: deterministic hardware checks, and service_install()
    is stubbed so a test can never install/modify a real system service, even
    if a bug caused the "run service install?" prompt to be answered yes.
    """
    monkeypatch.setattr(
        cli_mod, "check_deck_detected", lambda config_path=None: ("warn", "no device")
    )
    monkeypatch.setattr(cli_mod, "check_hid_openable", lambda: ("warn", "n/a"))
    monkeypatch.setattr(init_wizard.service_mod, "service_install", lambda: None)


def _fail_getpass(_prompt: str) -> str:
    raise AssertionError("getpass_func should not have been called in this test")


def _fail_input(_prompt: str) -> str:
    raise AssertionError("input_func should not have been called in this test")


# ---------------------------------------------------------------------------
# 1. Reachable server -> correct config.json + reports name/version
# ---------------------------------------------------------------------------


class TestReachableServerWritesConfig:
    def test_writes_config_and_reports_name_version(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "Found muxplex 'spark-1' running v0.13.0" in out

        final = config_mod.load_raw_config(config_path)
        assert final["server_url"] == "https://spark-1.test:8088"
        assert final["ca_file"] == ""


# ---------------------------------------------------------------------------
# 2. CA auto-fetch success
# ---------------------------------------------------------------------------


class TestCaAutoFetch:
    def test_ca_fetched_and_written_with_fingerprint(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        cert_bytes = (
            b"-----BEGIN CERTIFICATE-----\nFAKECERT\n-----END CERTIFICATE-----\n"
        )
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(200, content=cert_bytes),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0

        expected_ca_path = deck_home / ".config" / "muxplex-deck" / "muxplex-ca.crt"
        assert expected_ca_path.exists()
        assert expected_ca_path.read_bytes() == cert_bytes

        expected_fingerprint = ":".join(
            hashlib.sha256(cert_bytes).hexdigest().upper()[i : i + 2]
            for i in range(0, 64, 2)
        )
        assert expected_fingerprint in out
        assert "SHA-256 fingerprint" in out

        final = config_mod.load_raw_config(config_path)
        assert final["ca_file"] == str(expected_ca_path)


# ---------------------------------------------------------------------------
# 3. CA endpoint 404 + TLS working -> skipped cleanly
# ---------------------------------------------------------------------------


class TestCaSkippedWhenTlsAlreadyWorks:
    def test_ca_404_and_tls_ok_leaves_ca_file_unset(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "CA: not needed" in out

        final = config_mod.load_raw_config(config_path)
        assert not final["ca_file"]
        ca_path = deck_home / ".config" / "muxplex-deck" / "muxplex-ca.crt"
        assert not ca_path.exists()


# ---------------------------------------------------------------------------
# 4. CA endpoint 404 + TLS failing -> prompt for path, reject a leaf cert
# ---------------------------------------------------------------------------


class TestCaPromptRejectsLeafCert:
    def test_leaf_cert_rejected_then_valid_ca_accepted(
        self,
        deck_home: Path,
        config_path: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        tls_exc = httpx.ConnectError(
            "certificate verify failed: unable to get local issuer certificate"
        )
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": tls_exc,
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": _FakeCaResponse(200),
            },
        )

        leaf_path = tmp_path / "leaf.crt"
        leaf_path.write_text("fake leaf cert", encoding="utf-8")
        good_path = tmp_path / "good-ca.crt"
        good_path.write_text("fake ca cert", encoding="utf-8")

        # Exact-path lookup (NOT substring matching) -- pytest names tmp_path
        # after the test function, which itself contains "leaf", so a naive
        # `"leaf" in path` check would false-positive on *every* path here.
        openssl_results = {
            str(leaf_path): "X509v3 Basic Constraints:\nCA:FALSE\n",
            str(good_path): "X509v3 Basic Constraints:\nCA:TRUE\n",
        }

        def _fake_openssl(*args: Any, **kwargs: Any) -> _FakeCompletedProcess:
            cmd = args[0]
            path_arg = cmd[3]
            return _FakeCompletedProcess(0, stdout=openssl_results[path_arg])

        monkeypatch.setattr(cli_mod.subprocess, "run", _fake_openssl)

        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input([str(leaf_path), str(good_path), "n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "CA:FALSE" in out
        assert "LEAF" in out

        final = config_mod.load_raw_config(config_path)
        assert final["ca_file"] == str(good_path)


# ---------------------------------------------------------------------------
# 5. Federation key permissions + never logged
# ---------------------------------------------------------------------------


class TestFederationKeyProtection:
    def test_key_written_with_correct_modes_and_never_printed(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        secret = "SUPER-SECRET-FEDERATION-KEY-VALUE-42"
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: secret,
        )
        assert rc == 0

        captured = capsys.readouterr()
        assert secret not in captured.out
        assert secret not in captured.err

        key_path = deck_home / ".config" / "muxplex-deck" / "federation_key"
        assert key_path.exists()
        assert key_path.read_text(encoding="utf-8").strip() == secret

        key_mode = stat.S_IMODE(key_path.stat().st_mode)
        assert key_mode == 0o600
        dir_mode = stat.S_IMODE(key_path.parent.stat().st_mode)
        assert dir_mode == 0o700


# ---------------------------------------------------------------------------
# 5b. Federation key VALIDATION -- the actual bug this round of fixes closes:
# `init` used to write a pasted key without ever exercising it. `/api/
# instance-info` (used to verify the server, above) is unauthenticated and
# proves nothing about the key -- only an authenticated request can.
# ---------------------------------------------------------------------------


def _sessions_route_checking_bearer(accepted_key: str):
    """Route /api/sessions by the Authorization header actually sent --

    this proves the wizard is validating the REAL key value, not just
    calling the endpoint and assuming success.
    """

    def _respond(headers: dict[str, str] | None) -> _FakeCaResponse:
        token = (headers or {}).get("Authorization", "")
        if token == f"Bearer {accepted_key}":
            return _FakeCaResponse(200)
        return _FakeCaResponse(401)

    return _respond


class TestFederationKeyValidation:
    def test_rejected_key_loops_until_accepted(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": _sessions_route_checking_bearer("right-key"),
            },
        )
        getpass_sequence = iter(["wrong-key", "right-key"])
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: next(getpass_sequence),
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "rejected by server" in out.lower()
        assert "Try again" in out
        assert "verified against server" in out.lower()

        key_path = deck_home / ".config" / "muxplex-deck" / "federation_key"
        # The REJECTED key must never be persisted -- only the accepted one.
        assert key_path.read_text(encoding="utf-8").strip() == "right-key"

    def test_network_error_warns_but_does_not_fabricate_success(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """A network hiccup verifying the key is not evidence the key is

        wrong -- must warn honestly (never print a false "verified" check)
        but must not block init forever on something re-pasting can't fix.
        """
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": httpx.ConnectError("connection reset"),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "some-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "could not verify federation key" in out.lower()
        assert "verified against server" not in out.lower()

        key_path = deck_home / ".config" / "muxplex-deck" / "federation_key"
        assert key_path.read_text(encoding="utf-8").strip() == "some-key"


# ---------------------------------------------------------------------------
# 6. federation_enabled: false -> up-front warning
# ---------------------------------------------------------------------------


class TestFederationDisabledWarning:
    def test_federation_disabled_warns_up_front(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {
                        "name": "spark-1",
                        "version": "0.13.0",
                        "federation_enabled": False,
                    }
                ),
                "/api/ca": _FakeCaResponse(404),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "Federation is not enabled" in out
        assert "generate-federation-key" in out


# ---------------------------------------------------------------------------
# 7. Idempotent re-run preserves unrelated config keys
# ---------------------------------------------------------------------------


class TestIdempotentReRun:
    def test_rerun_preserves_sort_and_poll_interval(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        # Pre-existing config with customized unrelated fields + an existing
        # valid federation key file.
        key_path = deck_home / ".config" / "muxplex-deck" / "federation_key"
        key_path.parent.mkdir(parents=True)
        key_path.write_text("existing-key-value\n", encoding="utf-8")
        key_path.chmod(0o600)

        config_mod.save_raw_config(
            {
                **DEFAULT_CONFIG,
                "server_url": "https://spark-1.test:8088",
                "sort": "server",
                "poll_interval": 5.0,
            },
            config_path,
        )

        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
            },
        )

        rc = init_wizard.run_init(
            config_path,
            None,
            non_interactive=False,
            # "" keeps the default server URL; "" keeps the existing key;
            # "n" declines the service-install offer.
            input_func=_make_input(["", "", "n"]),
            getpass_func=_fail_getpass,
        )
        assert rc == 0

        final = config_mod.load_raw_config(config_path)
        assert final["sort"] == "server"
        assert final["poll_interval"] == 5.0
        assert final["server_url"] == "https://spark-1.test:8088"
        # Key file untouched (getpass_func was never called -- asserted via
        # _fail_getpass above; the on-disk content also should be unchanged).
        assert key_path.read_text(encoding="utf-8").strip() == "existing-key-value"


# ---------------------------------------------------------------------------
# 5c. Federation key "skip" -- a real, named exit. Round of fixes closed:
# the old prompt mixed a secret-paste and a local-file-path route through
# one hidden getpass call and printed a bare `<your-server>` placeholder
# with no indication of which machine the command ran on.
# ---------------------------------------------------------------------------


class TestFederationKeySkip:
    def test_skip_finishes_config_without_key_and_suppresses_service_offer(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
            },
        )
        # No "n" scripted for the service-install offer -- it must never be
        # asked at all when the key was skipped. If the wizard asks it
        # anyway, _make_input raises on the empty iterator.
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input([]),
            getpass_func=lambda _p: "skip",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "skipped" in out.lower()
        assert "muxplex-deck init" in out
        assert "incomplete without a federation key" in out.lower()
        assert "you're set up" not in out.lower()

        key_path = deck_home / ".config" / "muxplex-deck" / "federation_key"
        assert not key_path.exists()

    def test_blank_response_also_skips(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input([]),
            getpass_func=lambda _p: "",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "skipped" in out.lower()

    def test_prompt_shows_real_host_via_ssh_never_a_placeholder(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """The prompt must say which machine (ssh <host> ...) with the
        actual server hostname substituted -- never a bare remote path
        and never a `<your-server>` placeholder the wizard could have
        resolved itself.
        """
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(404),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input([]),
            getpass_func=lambda _p: "skip",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "ssh spark-1.test cat ~/.config/muxplex/federation_key" in out
        assert "<your-server>" not in out
        assert "scp <your-server>" not in out


# ---------------------------------------------------------------------------
# 5d. TLS-failure line -- interpretation first, raw exception only under
# MUXPLEX_DECK_VERBOSE. Same defect class as the federation-key "success
# marker for a step that didn't run": don't alarm with a raw `_ssl.c`
# detail and then retract it a line later as expected.
# ---------------------------------------------------------------------------


class TestTlsFailureLineIsInterpretedNotRaw:
    def test_default_hides_raw_exception_detail(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.delenv("MUXPLEX_DECK_VERBOSE", raising=False)
        tls_exc = httpx.ConnectError(
            "certificate verify failed: unable to get local issuer certificate "
            "(_ssl.c:1081)"
        )
        cert_bytes = (
            b"-----BEGIN CERTIFICATE-----\nFAKECERT\n-----END CERTIFICATE-----\n"
        )
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": tls_exc,
                "/api/ca": _FakeCaResponse(200, content=cert_bytes),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        # Scope the assertion to the server-validation step this fix
        # touches (`_validate_server`) -- `check_server_reachable`'s own
        # TLS message (used later, at the post-write verification step)
        # is a separate, unfixed code path with different phrasing and is
        # out of scope for this fix.
        validation_step = out.split("\nConfig saved:")[0]
        assert "own certificate authority" in validation_step.lower()
        assert "_ssl.c" not in validation_step

    def test_verbose_env_var_shows_raw_exception_detail(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setenv("MUXPLEX_DECK_VERBOSE", "1")
        tls_exc = httpx.ConnectError(
            "certificate verify failed: unable to get local issuer certificate "
            "(_ssl.c:1081)"
        )
        cert_bytes = (
            b"-----BEGIN CERTIFICATE-----\nFAKECERT\n-----END CERTIFICATE-----\n"
        )
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": tls_exc,
                "/api/ca": _FakeCaResponse(200, content=cert_bytes),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "_ssl.c" in out


# ---------------------------------------------------------------------------
# 5e. CA fingerprint block -- must say which machine (real hostname, not
# a bare POSIX path unrunnable on the client), and must fit 80 columns.
# ---------------------------------------------------------------------------


class TestCaFingerprintBlockNamesTheHost:
    def test_verify_command_is_ssh_wrapped_with_real_host(
        self,
        deck_home: Path,
        config_path: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        cert_bytes = (
            b"-----BEGIN CERTIFICATE-----\nFAKECERT\n-----END CERTIFICATE-----\n"
        )
        _patch_httpx(
            monkeypatch,
            {
                "/api/instance-info": _FakeJsonResponse(
                    {"name": "spark-1", "version": "0.13.0", "federation_enabled": True}
                ),
                "/api/ca": _FakeCaResponse(200, content=cert_bytes),
                "/api/sessions": _FakeCaResponse(200),
            },
        )
        rc = init_wizard.run_init(
            config_path,
            "https://spark-1.test:8088",
            non_interactive=False,
            input_func=_make_input(["n"]),
            getpass_func=lambda _p: "test-federation-key",
        )
        out = capsys.readouterr().out
        assert rc == 0
        assert "ssh spark-1.test openssl x509" in out
        # The verify-command lines specifically must fit a standard
        # 80-column terminal (unlike the CA-path line above them, which
        # necessarily contains a long filesystem path and isn't part of
        # this fix -- note the tmp_path used to isolate this test happens
        # to contain the substring "ssh" itself, from the test's own
        # name, so match on the stripped line PREFIX, not "contains").
        for line in out.splitlines():
            stripped = line.strip()
            if stripped.startswith(("ssh ", "-in ")):
                assert len(line) <= 80, f"line exceeds 80 columns: {line!r}"


# ---------------------------------------------------------------------------
# 8. --non-interactive with missing required input
# ---------------------------------------------------------------------------


class TestNonInteractiveMissingInput:
    def test_missing_server_url_fails_clearly_and_writes_nothing(
        self,
        deck_home: Path,
        config_path: str,
        capsys: pytest.CaptureFixture,
    ) -> None:
        rc = init_wizard.run_init(
            config_path,
            None,
            non_interactive=True,
            input_func=_fail_input,
            getpass_func=_fail_getpass,
        )
        err = capsys.readouterr().err
        assert rc == 1
        assert "ERROR" in err
        assert "SERVER_URL" in err
        assert not Path(config_path).exists()


# ---------------------------------------------------------------------------
# 9. Ctrl-C / EOF mid-prompt writes no partial config
# ---------------------------------------------------------------------------


class TestAbortMidPrompt:
    @pytest.mark.parametrize("exc_cls", [KeyboardInterrupt, EOFError])
    def test_abort_on_first_prompt_writes_no_config(
        self,
        exc_cls: type[BaseException],
        deck_home: Path,
        config_path: str,
        capsys: pytest.CaptureFixture,
    ) -> None:
        def _raise(_prompt: str) -> str:
            raise exc_cls()

        rc = init_wizard.run_init(
            config_path,
            None,
            non_interactive=False,
            input_func=_raise,
            getpass_func=_fail_getpass,
        )
        err = capsys.readouterr().err
        assert rc == 130
        assert "Aborted" in err
        assert not Path(config_path).exists()
