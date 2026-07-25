"""`muxplex-deck update` -- install command construction, subprocess fully mocked.

No real `uv`/`pip`/`systemctl`/`launchctl` is ever invoked: `subprocess.run`
is monkeypatched throughout. Proves: uv-present path uses
`uv tool install --force git+<repo>`, uv-absent path falls back to
`pip install --upgrade git+<repo>`, and doctor() is called on success.
"""

from __future__ import annotations

from typing import Any

import pytest

from muxplex_deck import cli


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRun:
    def __init__(self, results: dict[tuple, _FakeResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._results = results or {}

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeResult:
        self.calls.append(list(argv))
        key = tuple(argv)
        return self._results.get(key, _FakeResult(returncode=0))


@pytest.fixture(autouse=True)
def no_real_service_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: never let a real service_install/service_restart run."""
    monkeypatch.setattr(cli, "doctor", lambda *a, **k: 0)


class TestUvPresent:
    def test_uses_uv_tool_install_force_git(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)

        cli.update()

        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/uv"]]
        assert install_calls == [
            ["/usr/bin/uv", "tool", "install", "--force", f"git+{cli._REPO_URL}"]
        ]

    def test_install_failure_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingRun(
            results={
                (
                    "/usr/bin/uv",
                    "tool",
                    "install",
                    "--force",
                    f"git+{cli._REPO_URL}",
                ): _FakeResult(returncode=1, stderr="boom")
            }
        )
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)

        with pytest.raises(SystemExit) as excinfo:
            cli.update()
        assert excinfo.value.code == 1


class TestUvAbsentPipFallback:
    def test_falls_back_to_pip_install_upgrade_git(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: None)
        monkeypatch.setattr(cli, "_find_pip", lambda: "/usr/bin/pip3")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)

        cli.update()

        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/pip3"]]
        assert install_calls == [
            ["/usr/bin/pip3", "install", "--upgrade", f"git+{cli._REPO_URL}"]
        ]

    def test_neither_uv_nor_pip_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: None)
        monkeypatch.setattr(cli, "_find_pip", lambda: None)
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)

        with pytest.raises(SystemExit) as excinfo:
            cli.update()
        assert excinfo.value.code == 1


class TestServiceLifecycleAroundUpdate:
    def test_active_service_is_stopped_and_restarted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: True)
        monkeypatch.setattr(cli.sys, "platform", "linux")

        restart_calls: list[bool] = []
        monkeypatch.setattr(
            "muxplex_deck.service.service_restart", lambda: restart_calls.append(True)
        )
        monkeypatch.setattr("muxplex_deck.service.service_install", lambda: None)

        cli.update()

        assert ["systemctl", "--user", "stop", "muxplex-deck"] in recorder.calls
        assert restart_calls == [True]

    def test_inactive_service_is_never_stopped_or_restarted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)

        restart_calls: list[bool] = []
        monkeypatch.setattr(
            "muxplex_deck.service.service_restart", lambda: restart_calls.append(True)
        )

        cli.update()

        stop_calls = [c for c in recorder.calls if "stop" in c]
        assert stop_calls == []
        assert restart_calls == []

    def test_calls_doctor_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)

        doctor_calls: list[bool] = []
        monkeypatch.setattr(
            cli, "doctor", lambda *a, **k: doctor_calls.append(True) or 0
        )

        cli.update()

        assert doctor_calls == [True]
