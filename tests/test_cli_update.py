"""`muxplex-deck update` -- install command construction, subprocess fully mocked.

No real `uv`/`pip`/`systemctl`/`launchctl` is ever invoked: `subprocess.run`
is monkeypatched throughout, and `_get_install_info`/`_check_for_update` are
monkeypatched (directly, or via the `_default_install_info` autouse fixture)
so no test depends on this dev checkout's own real install metadata
(editable) or hits the network/git.

Covers the source-aware behavior added to close the "doctor recommends
update, update reverts a deliberate pypi install back to git" trap:
  - `_default_install_info` defaults every test to a `git` install with an
    update available -- preserves the pre-fix behavior/assertions for tests
    that don't care about install source.
  - `TestInstallSourceSelectsTarget` proves pypi -> bare package name,
    git/unknown -> `git+<repo>` (unchanged).
  - `TestAlreadyUpToDateSkipsReinstall` proves the new version-gate: no
    subprocess calls at all when already current, `--force` bypasses it.
  - `TestEditableInstallNeverReinstalled` proves an editable (dev) install
    is left alone unconditionally, even under `--force`.
  - `TestUnreachablePyPIDegradesGracefully` proves a real (unmocked)
    `_check_for_update` facing a network failure still lets the update
    proceed rather than blocking or crashing.
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


@pytest.fixture(autouse=True)
def _default_install_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to a `git` install with an update available.

    Without this, `update()` would call the REAL `_get_install_info()` /
    `_check_for_update()` -- which, in this dev checkout, report an
    `editable` install and thus "already up to date" -- silently skipping
    the reinstall every test below assumes happens. Tests that need a
    different source/status override these two functions themselves;
    a later `monkeypatch.setattr` in the test body simply overrides this
    fixture's patch for that test.
    """
    monkeypatch.setattr(
        cli,
        "_get_install_info",
        lambda: {
            "source": "git",
            "version": "0.0.0",
            "commit": "abc123",
            "url": cli._REPO_URL,
        },
    )
    monkeypatch.setattr(
        cli,
        "_check_for_update",
        lambda info: (True, "update available (abc123 -> def456)"),
    )


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

    def test_active_service_on_windows_is_stopped_via_schtasks_not_systemctl(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Was a LIVE BUG: `update()`'s stop block hardcoded

        launchctl/systemctl (`else: subprocess.run(["systemctl", ...])`),
        which raises `FileNotFoundError` on Windows -- `check=False` only
        suppresses a nonzero *exit*, not a failed *exec*. Latent only
        because `_service_is_active()` returned False on Windows before
        Task Scheduler support existed. Now that `service_stop()` dispatches
        per platform, an active Windows service must be stopped via
        `schtasks /End`, never `systemctl`.
        """
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: True)
        monkeypatch.setattr(cli.sys, "platform", "win32")

        restart_calls: list[bool] = []
        monkeypatch.setattr(
            "muxplex_deck.service.service_restart", lambda: restart_calls.append(True)
        )
        monkeypatch.setattr("muxplex_deck.service.service_install", lambda: None)

        cli.update()  # must not raise FileNotFoundError

        stop_calls = [c for c in recorder.calls if c[:2] == ["schtasks", "/End"]]
        assert len(stop_calls) == 1
        assert not any(c and c[0] == "systemctl" for c in recorder.calls)
        assert restart_calls == [True]


# ---------------------------------------------------------------------------
# Install-source awareness -- the fix for the "doctor tells you to run
# update; update reverts your pypi install back to git" trap.
# ---------------------------------------------------------------------------


class TestInstallSourceSelectsTarget:
    def test_pypi_source_uses_bare_package_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {"source": "pypi", "version": "0.4.0", "commit": None, "url": None},
        )
        monkeypatch.setattr(
            cli,
            "_check_for_update",
            lambda info: (True, "update available (v0.4.0 -> v0.5.0)"),
        )

        cli.update()

        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/uv"]]
        assert install_calls == [
            ["/usr/bin/uv", "tool", "install", "--force", "muxplex-deck"]
        ]

    def test_git_source_still_uses_git_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {
                "source": "git",
                "version": "0.0.0",
                "commit": "abc123",
                "url": cli._REPO_URL,
            },
        )
        monkeypatch.setattr(
            cli,
            "_check_for_update",
            lambda info: (True, "update available (abc123 -> def456)"),
        )

        cli.update()

        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/uv"]]
        assert install_calls == [
            ["/usr/bin/uv", "tool", "install", "--force", f"git+{cli._REPO_URL}"]
        ]

    def test_unknown_source_falls_back_to_git_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Preserves the pre-fix default for the rare ambiguous-source case."""
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {
                "source": "unknown",
                "version": "0.0.0",
                "commit": None,
                "url": None,
            },
        )
        monkeypatch.setattr(
            cli,
            "_check_for_update",
            lambda info: (True, "unknown install source -- could not check"),
        )

        cli.update()

        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/uv"]]
        assert install_calls == [
            ["/usr/bin/uv", "tool", "install", "--force", f"git+{cli._REPO_URL}"]
        ]


class TestAlreadyUpToDateSkipsReinstall:
    def test_pypi_up_to_date_skips_install_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {"source": "pypi", "version": "0.4.0", "commit": None, "url": None},
        )
        monkeypatch.setattr(
            cli, "_check_for_update", lambda info: (False, "up to date (v0.4.0)")
        )

        cli.update()

        # No subprocess calls whatsoever -- not even a service stop attempt.
        assert recorder.calls == []

    def test_git_same_commit_skips_install_entirely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {
                "source": "git",
                "version": "0.0.0",
                "commit": "abc123",
                "url": cli._REPO_URL,
            },
        )
        monkeypatch.setattr(
            cli, "_check_for_update", lambda info: (False, "up to date (commit abc123)")
        )

        cli.update()

        assert recorder.calls == []

    def test_force_bypasses_up_to_date_skip_and_skips_the_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {"source": "pypi", "version": "0.4.0", "commit": None, "url": None},
        )
        check_calls: list[bool] = []
        monkeypatch.setattr(
            cli,
            "_check_for_update",
            lambda info: check_calls.append(True) or (False, "up to date (v0.4.0)"),
        )

        cli.update(force=True)

        # --force must skip the version check entirely, not just override its result.
        assert check_calls == []
        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/uv"]]
        assert install_calls == [
            ["/usr/bin/uv", "tool", "install", "--force", "muxplex-deck"]
        ]


class TestEditableInstallNeverReinstalled:
    def test_editable_source_takes_no_action(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {
                "source": "editable",
                "version": "0.4.0",
                "commit": None,
                "url": None,
            },
        )
        check_calls: list[bool] = []
        monkeypatch.setattr(
            cli,
            "_check_for_update",
            lambda info: check_calls.append(True) or (False, "editable install"),
        )

        cli.update()

        assert recorder.calls == []
        # Editable short-circuits before even consulting the version check.
        assert check_calls == []

    def test_editable_source_ignores_force(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {
                "source": "editable",
                "version": "0.4.0",
                "commit": None,
                "url": None,
            },
        )

        cli.update(force=True)

        assert recorder.calls == []


class TestUnreachablePyPIDegradesGracefully:
    def test_httpx_failure_still_lets_the_update_proceed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end through the REAL `_check_for_update`: httpx.get is the
        only thing mocked (to raise), proving an offline/unreachable PyPI
        degrades to "upgrade to be safe" rather than blocking or crashing
        the update.
        """
        import httpx

        recorder = _RecordingRun()
        monkeypatch.setattr(cli.subprocess, "run", recorder)
        monkeypatch.setattr(cli, "_find_uv", lambda: "/usr/bin/uv")
        monkeypatch.setattr(cli, "_service_is_active", lambda: False)
        monkeypatch.setattr(
            cli,
            "_get_install_info",
            lambda: {"source": "pypi", "version": "0.4.0", "commit": None, "url": None},
        )

        def _raise(*_a: object, **_k: object) -> None:
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "get", _raise)

        cli.update()

        install_calls = [c for c in recorder.calls if c[:1] == ["/usr/bin/uv"]]
        assert install_calls == [
            ["/usr/bin/uv", "tool", "install", "--force", "muxplex-deck"]
        ]
