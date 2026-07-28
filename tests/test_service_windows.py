"""Native Windows Task Scheduler backend -- `service.py`'s `_win_*` implementations.

See WINDOWS_NATIVE_SPEC.md section 1 for the full design rationale (why a
real Windows Service is disqualified, why Task Scheduler at-logon in the
interactive user's own context is used instead, and why COM -- not
`schtasks /Query` text -- is the only correctness-safe way to read task
state).

Pure unit tests throughout: `subprocess.run` is always mocked (never a real
`schtasks.exe`/`powershell.exe` spawn -- see `tests/conftest.py` Rail 4),
and `sys.platform` is monkeypatched to `"win32"` only where a function
actually branches on it (`service_is_active`/`service_main_pid`/
`service_is_installed`/`service_manager_available`/the public dispatchers);
the `_win_*` private implementations themselves are platform-agnostic pure
functions/subprocess wrappers and are exercised directly regardless of the
host OS running the suite (the same pattern `test_cli_service.py` already
uses for `_systemd_*`/`_launchd_*`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from muxplex_deck import service as service_mod


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _RecordingRun:
    """Stand-in for subprocess.run that records every invocation.

    `calls` keeps the old shape (`list[list[str]]`, argv only) so every
    existing assertion keeps working unchanged; `calls_with_kwargs` is
    additive, recording the full `(argv, kwargs)` pair for tests that need
    to assert on kwargs (e.g. `stdin=subprocess.DEVNULL`).
    """

    def __init__(
        self, results: dict[tuple, _FakeCompletedProcess] | None = None
    ) -> None:
        self.calls: list[list[str]] = []
        self.calls_with_kwargs: list[tuple[list[str], dict[str, Any]]] = []
        self._results = results or {}

    def __call__(self, argv: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        self.calls.append(list(argv))
        self.calls_with_kwargs.append((list(argv), dict(kwargs)))
        return self._results.get(tuple(argv), _FakeCompletedProcess(0))


@pytest.fixture
def recording_run(monkeypatch: pytest.MonkeyPatch) -> _RecordingRun:
    recorder = _RecordingRun()
    monkeypatch.setattr(service_mod.subprocess, "run", recorder)
    return recorder


# ---------------------------------------------------------------------------
# _parse_win_task_query -- pure, must never raise (WINDOWS_NATIVE_SPEC.md
# section 1.4 / section 8: "MISSING", "OK 4 12345", "OK 3 0", garbage, empty).
# ---------------------------------------------------------------------------


class TestParseWinTaskQuery:
    def test_missing_task(self) -> None:
        info = service_mod._parse_win_task_query("MISSING")
        assert info == service_mod.WinTaskInfo(exists=False, state=None, pid=None)

    def test_running_with_pid(self) -> None:
        info = service_mod._parse_win_task_query("OK 4 12345")
        assert info.exists is True
        assert info.state == 4
        assert info.pid == 12345

    def test_registered_not_running_zero_pid_means_none(self) -> None:
        """Task Scheduler's own convention: pid 0 means no running instance."""
        info = service_mod._parse_win_task_query("OK 3 0")
        assert info.exists is True
        assert info.state == 3
        assert info.pid is None

    def test_garbage_output_reads_as_not_installed(self) -> None:
        """Never raise; unparseable output reads as the conservative default."""
        info = service_mod._parse_win_task_query("asdkjasd garbage output\n")
        assert info == service_mod.WinTaskInfo(exists=False, state=None, pid=None)

    def test_empty_output_reads_as_not_installed(self) -> None:
        info = service_mod._parse_win_task_query("")
        assert info == service_mod.WinTaskInfo(exists=False, state=None, pid=None)

    def test_whitespace_only_output_reads_as_not_installed(self) -> None:
        info = service_mod._parse_win_task_query("   \n  ")
        assert info == service_mod.WinTaskInfo(exists=False, state=None, pid=None)

    def test_ok_with_unparseable_state_and_pid_reads_as_indeterminate(self) -> None:
        """Exists (it wasn't MISSING), but state/pid can't be trusted -- never guess."""
        info = service_mod._parse_win_task_query("OK notanumber alsonotanumber")
        assert info.exists is True
        assert info.state is None
        assert info.pid is None

    def test_ok_alone_with_no_state_or_pid_tokens(self) -> None:
        info = service_mod._parse_win_task_query("OK")
        assert info.exists is True
        assert info.state is None
        assert info.pid is None


# ---------------------------------------------------------------------------
# _win_task_query -- one PowerShell/COM spawn answers all three predicates.
# Never raises: missing powershell.exe or a timeout both read as "not
# installed", matching service_main_pid()'s existing never-raise contract.
# ---------------------------------------------------------------------------


class TestWinTaskQuery:
    def test_parses_real_subprocess_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(0, stdout="OK 4 999\n"),
        )
        info = service_mod._win_task_query()
        assert info == service_mod.WinTaskInfo(exists=True, state=4, pid=999)

    def test_missing_powershell_reads_as_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a: Any, **k: Any) -> Any:
            raise FileNotFoundError("no powershell.exe")

        monkeypatch.setattr(service_mod.subprocess, "run", _raise)
        assert service_mod._win_task_query() == service_mod.WinTaskInfo(
            exists=False, state=None, pid=None
        )

    def test_timeout_reads_as_not_installed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import subprocess as real_subprocess

        def _raise(*a: Any, **k: Any) -> Any:
            raise real_subprocess.TimeoutExpired(cmd="powershell.exe", timeout=10.0)

        monkeypatch.setattr(service_mod.subprocess, "run", _raise)
        assert service_mod._win_task_query() == service_mod.WinTaskInfo(
            exists=False, state=None, pid=None
        )

    def test_query_uses_the_current_task_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_WIN_TASK_NAME", "my-test-task")
        recorder = _RecordingRun()
        monkeypatch.setattr(service_mod.subprocess, "run", recorder)

        service_mod._win_task_query()

        assert len(recorder.calls) == 1
        command = recorder.calls[0][-1]
        assert "my-test-task" in command
        assert "Schedule.Service" in command


# ---------------------------------------------------------------------------
# service_is_installed / service_is_active / service_main_pid -- win32
# branches, dispatched via _win_task_query().
# ---------------------------------------------------------------------------


class TestWindowsPredicates:
    def test_is_installed_true_when_task_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=True, state=3, pid=None),
        )
        assert service_mod.service_is_installed() is True

    def test_is_installed_false_when_task_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=False, state=None, pid=None),
        )
        assert service_mod.service_is_installed() is False

    def test_is_active_true_only_when_state_is_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(
                exists=True, state=service_mod._WIN_TASK_STATE_RUNNING, pid=555
            ),
        )
        assert service_mod.service_is_active() is True

    def test_is_active_false_when_registered_but_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=True, state=3, pid=None),
        )
        assert service_mod.service_is_active() is False

    def test_main_pid_always_none_even_when_task_running_with_a_pid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """VERIFIED FALSE on real hardware: `EnginePID` is the Task

        Scheduler engine-HOST process, never the sidecar's own pid (see
        `service_main_pid()`'s docstring) -- so this must return None
        UNCONDITIONALLY on Windows, even when `_win_task_query()` reports a
        running task with a (real, but not-ours) pid. Returning that pid
        would be fabricating an authoritative-looking answer that is
        actively wrong, not just imprecise.
        """
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(
                exists=True, state=service_mod._WIN_TASK_STATE_RUNNING, pid=4242
            ),
        )
        assert service_mod.service_main_pid() is None

    def test_main_pid_none_when_not_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=True, state=3, pid=None),
        )
        assert service_mod.service_main_pid() is None


# ---------------------------------------------------------------------------
# service_manager_available -- Task Scheduler makes this True on Windows now
# (was False before this increment; see AGENTS.md v0.5.2 crash-loop
# incident on why callers must never OFFER an impossible action).
# ---------------------------------------------------------------------------


class TestServiceManagerAvailableWindows:
    def test_true_on_windows_even_without_systemctl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: False)
        assert service_mod.service_manager_available() is True


# ---------------------------------------------------------------------------
# _resolve_pythonw -- WINDOWS_NATIVE_SPEC.md section 1.2 constraint 1:
# pythonw.exe (GUI subsystem, no console window), falling back to
# sys.executable (never fails install over this).
# ---------------------------------------------------------------------------


class TestResolvePythonw:
    def test_prefers_pythonw_next_to_executable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        python_exe = tmp_path / "python.exe"
        python_exe.write_text("")
        pythonw_exe = tmp_path / "pythonw.exe"
        pythonw_exe.write_text("")
        monkeypatch.setattr(service_mod.sys, "executable", str(python_exe))

        assert service_mod._resolve_pythonw() == str(pythonw_exe)

    def test_falls_back_to_sys_executable_when_pythonw_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        python_exe = tmp_path / "python.exe"
        python_exe.write_text("")
        monkeypatch.setattr(service_mod.sys, "executable", str(python_exe))

        assert service_mod._resolve_pythonw() == str(python_exe)


# ---------------------------------------------------------------------------
# Task XML generation -- golden assertions for every load-bearing setting
# from WINDOWS_NATIVE_SPEC.md section 1.2. Written as UTF-16 LE + BOM (the
# one deliberate exception to this repo's blanket encoding="utf-8" rule --
# see `_write_win_task_xml`'s docstring; UNVERIFIED on real hardware).
# ---------------------------------------------------------------------------


class TestWinTaskXml:
    def _xml(self) -> str:
        return service_mod._win_task_xml(
            pythonw_path=r"C:\Users\bob\.local\share\uv\tools\muxplex-deck\Scripts\pythonw.exe",
            log_file=Path(r"C:\Users\bob\.local\state\muxplex-deck\muxplex-deck.log"),
            user_id=r"DESKTOP-X\bob",
        )

    def test_contains_no_stored_password_logon_trigger(self) -> None:
        xml = self._xml()
        assert "<LogonTrigger>" in xml
        assert r"DESKTOP-X\bob" in xml

    def test_contains_interactive_token_and_least_privilege(self) -> None:
        xml = self._xml()
        assert "<LogonType>InteractiveToken</LogonType>" in xml
        assert "<RunLevel>LeastPrivilege</RunLevel>" in xml

    def test_contains_execution_time_limit_pt0s(self) -> None:
        """Critical: default is 3 DAYS, after which Task Scheduler kills the task."""
        xml = self._xml()
        assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml

    def test_contains_both_battery_flags_disabled(self) -> None:
        """Critical on laptops: both default to true, which would refuse to

        start (or kill mid-session) on battery.
        """
        xml = self._xml()
        assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
        assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml

    def test_contains_ignore_new_repetition_trigger(self) -> None:
        """One restart mechanism covering every death mode -- not

        <RestartOnFailure>, which only fires on a nonzero exit.
        """
        xml = self._xml()
        assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
        assert "<Interval>PT1M</Interval>" in xml

    def test_contains_start_when_available_and_allow_start_on_demand(self) -> None:
        xml = self._xml()
        assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
        assert "<AllowStartOnDemand>true</AllowStartOnDemand>" in xml

    def test_never_hidden_never_wakes_machine(self) -> None:
        xml = self._xml()
        assert "<Hidden>false</Hidden>" in xml
        assert "<WakeToRun>false</WakeToRun>" in xml

    def test_action_has_no_cmd_wrapper_and_no_shell_redirection(self) -> None:
        """The PID contract (WINDOWS_NATIVE_SPEC.md section 1.2 constraint 2):

        a cmd.exe wrapper would make EnginePID the wrapper's pid, not the
        sidecar's.
        """
        xml = self._xml()
        assert "cmd.exe" not in xml
        assert ">" not in xml.split("<Arguments>")[1].split("</Arguments>")[0].replace(
            "&gt;", ""
        )
        assert (
            r"C:\Users\bob\.local\share\uv\tools\muxplex-deck\Scripts\pythonw.exe"
            in xml
        )
        assert "-m muxplex_deck run --log-file" in xml

    def test_xml_special_characters_are_escaped(self) -> None:
        xml = service_mod._win_task_xml(
            pythonw_path=r"C:\Users\bob & alice\pythonw.exe",
            log_file=Path(r"C:\Users\bob\muxplex-deck.log"),
            user_id="bob",
        )
        assert "bob & alice" not in xml
        assert "bob &amp; alice" in xml


class TestWriteWinTaskXml:
    def test_writes_utf16_le_with_bom(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "nested" / "muxplex-deck-task.xml"
        service_mod._write_win_task_xml(target, "<Task></Task>")

        raw = target.read_bytes()
        assert raw[:2] == b"\xff\xfe"  # BOM, little-endian
        assert raw.decode("utf-16-le").endswith("<Task></Task>")


class TestWinTaskArguments:
    def test_arguments_string_has_no_shell_wrapper(self) -> None:
        args = service_mod._win_task_arguments(Path(r"C:\logs\muxplex-deck.log"))
        assert args == r'-m muxplex_deck run --log-file "C:\logs\muxplex-deck.log"'
        assert "cmd.exe" not in args
        assert "&&" not in args


# ---------------------------------------------------------------------------
# _win_install / _win_uninstall / _win_start / _win_stop / _win_restart /
# _win_status / _win_logs -- narrated through report.py, same as
# systemd/launchd (see service.py's module docstring).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_win_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the Windows task-xml/log paths under the per-test tmp dir.

    `_win_task_xml_path()`/`_win_default_log_path()` derive from
    `statusfile.default_status_dir()`, which the autouse `XDG_STATE_HOME`
    rail (conftest.py Rail 3) already redirects -- this fixture just makes
    that explicit for readability in this file.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


class TestWinInstall:
    def test_gated_on_config_ready_same_as_systemd_launchd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """Never register a task whose config is known-broken -- IgnoreNew +

        PT1M would relaunch it forever (AGENTS.md's 1113-restart incident,
        same class of bug on this platform).
        """
        monkeypatch.setattr(
            service_mod, "_config_ready", lambda: (False, "Config file not found: X")
        )
        monkeypatch.setattr(service_mod, "_resolve_pythonw", lambda: "pythonw.exe")

        service_mod._win_install()

        assert not any(c[:2] == ["schtasks", "/Create"] for c in recording_run.calls)
        out = capsys.readouterr().out
        assert "Config file not found: X" in out
        assert "muxplex-deck init" in out
        # The XML is still written (harmless) -- matches systemd/launchd.
        assert service_mod._win_task_xml_path().exists()

    def test_ready_config_registers_and_starts_the_task(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))
        monkeypatch.setattr(service_mod, "_resolve_pythonw", lambda: "pythonw.exe")
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        service_mod._win_install()

        create_calls = [
            c for c in recording_run.calls if c[:2] == ["schtasks", "/Create"]
        ]
        assert len(create_calls) == 1
        # VERIFIED HANG on real hardware: `/RU <user>` without `/RP` makes
        # schtasks prompt INTERACTIVELY for a password on stdin, no matter
        # the logon type (confirmed by Microsoft's own docs -- see
        # `_win_install()`'s comment). The XML already carries the user's
        # identity (`<Principals><Principal><UserId>`), so `/RU` must never
        # be passed on the command line at all.
        assert "/RU" not in create_calls[0]
        assert not any(a == "cmd.exe" for a in create_calls[0])
        run_calls = [c for c in recording_run.calls if c[:2] == ["schtasks", "/Run"]]
        assert len(run_calls) == 1

        # No Windows subprocess call may inherit our stdin -- defense in
        # depth against exactly the hang above, for any future prompt.
        for argv, kwargs in recording_run.calls_with_kwargs:
            assert kwargs.get("stdin") is service_mod.subprocess.DEVNULL, (
                f"{argv} did not close stdin"
            )

        out = capsys.readouterr().out
        assert "Registered + started the task" in out
        # The honest trade-offs must be surfaced, not buried.
        assert "logon" in out
        assert "60s" in out

    def test_create_failure_reports_diagnostic_and_exits_nonzero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))
        monkeypatch.setattr(service_mod, "_resolve_pythonw", lambda: "pythonw.exe")

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["schtasks", "/Create"]:
                return _FakeCompletedProcess(1, stderr="ERROR: Access is denied.")
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        with pytest.raises(SystemExit) as exc_info:
            service_mod._win_install()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Access is denied" in out

    def test_missing_pythonw_warns_but_does_not_fail_install(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        monkeypatch.setattr(service_mod, "_config_ready", lambda: (True, None))
        monkeypatch.setattr(service_mod, "_resolve_pythonw", lambda: "python.exe")
        monkeypatch.setattr(service_mod, "service_is_active", lambda: True)

        service_mod._win_install()

        out = capsys.readouterr().out
        assert "pythonw.exe not found" in out
        assert "console window" in out
        # Still proceeds to register the task -- never fails over this.
        create_calls = [
            c for c in recording_run.calls if c[:2] == ["schtasks", "/Create"]
        ]
        assert len(create_calls) == 1


class TestWinUninstall:
    def test_uninstall_stops_deletes_and_removes_xml(
        self,
        monkeypatch: pytest.MonkeyPatch,
        recording_run: _RecordingRun,
        capsys: pytest.CaptureFixture,
    ) -> None:
        xml_path = service_mod._win_task_xml_path()
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text("x", encoding="utf-8")

        service_mod._win_uninstall()

        assert ["schtasks", "/End", "/TN", service_mod._WIN_TASK_NAME] in [
            c for c in recording_run.calls
        ]
        assert any(c[:2] == ["schtasks", "/Delete"] for c in recording_run.calls)
        assert not xml_path.exists()

        out = capsys.readouterr().out
        assert "Removed task XML" in out

    def test_uninstall_reports_not_registered_gracefully(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            return _FakeCompletedProcess(1)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._win_uninstall()

        out = capsys.readouterr().out
        assert "Was not running" in out
        assert "Was not registered" in out


class TestWinStart:
    def test_start_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        service_mod._win_start()
        out = capsys.readouterr().out
        assert "Started the task" in out

    def test_start_failure_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod.subprocess,
            "run",
            lambda *a, **k: _FakeCompletedProcess(1, stderr="ERROR: task not found"),
        )
        with pytest.raises(SystemExit) as exc_info:
            service_mod._win_start()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "task not found" in out


class TestWinStop:
    def test_stop_is_silent_and_ignores_failure(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Consistent with systemd/launchd's silent stop() -- nothing of ours

        to report, and `schtasks /End` failing (task not running) is not an
        error worth surfacing.
        """
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(1)
        )
        service_mod._win_stop()  # must not raise
        out = capsys.readouterr().out
        assert out == ""


class TestWinWaitForTaskStopped:
    """`_win_wait_for_task_stopped()` -- the fix for the real-hardware bug

    where `service restart` left the task registered but NOT running
    (state 3): `_win_restart()` used to issue `schtasks /Run` immediately
    after `/End`, racing Task Scheduler's own internal "is this task
    running" bookkeeping and losing the new run request to
    `MultipleInstancesPolicy=IgnoreNew`. This function closes that race by
    polling `_win_task_query()` directly (never `service_is_active()` --
    see the function's own docstring for why) until the state is no longer
    `_WIN_TASK_STATE_RUNNING`.
    """

    def test_returns_true_immediately_when_already_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=True, state=3, pid=None),
        )
        assert service_mod._win_wait_for_task_stopped() is True

    def test_polls_until_state_transitions_from_running_to_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_WIN_STOP_POLL_INTERVAL_SECONDS", 0.001)
        calls = {"n": 0}

        def _fake_query() -> service_mod.WinTaskInfo:
            calls["n"] += 1
            if calls["n"] < 3:
                return service_mod.WinTaskInfo(
                    exists=True, state=service_mod._WIN_TASK_STATE_RUNNING, pid=111
                )
            return service_mod.WinTaskInfo(exists=True, state=3, pid=None)

        monkeypatch.setattr(service_mod, "_win_task_query", _fake_query)

        assert service_mod._win_wait_for_task_stopped() is True
        assert calls["n"] == 3

    def test_returns_false_after_timeout_when_always_reported_running(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(service_mod, "_WIN_STOP_POLL_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(service_mod, "_WIN_STOP_POLL_INTERVAL_SECONDS", 0.005)
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(
                exists=True, state=service_mod._WIN_TASK_STATE_RUNNING, pid=111
            ),
        )
        assert service_mod._win_wait_for_task_stopped() is False

    def test_unqueryable_task_reads_as_stopped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Conservative default: cannot determine -> not running, matching

        `_parse_win_task_query()`'s own convention elsewhere in this module.
        """
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=False, state=None, pid=None),
        )
        assert service_mod._win_wait_for_task_stopped() is True


class TestWinRestart:
    def test_restart_waits_for_fresh_status_before_reporting_success(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The v0.5.3 restart-race fix, preserved on Windows -- but via a

        BASELINE-PID diff (WINDOWS_NATIVE_SPEC.md section 1.4's live-pid
        plan is impossible on Windows; see `service_main_pid()`'s
        docstring). `_win_restart()` reads the OLD pid (111) before
        stopping; once the "new" process's write (999) appears, that's a
        genuine pid change -- never claim success before that's observed.
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.001)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )

        # First read (the pre-stop baseline) sees the OLD process's pid;
        # every read after that sees the NEW process's -- simulating an
        # actual restart with a genuinely different pid.
        calls = {"n": 0}

        def _fake_read_status(path: Path | None = None) -> dict[str, Any]:
            calls["n"] += 1
            return {"pid": 111} if calls["n"] == 1 else {"pid": 999}

        monkeypatch.setattr(statusfile_mod, "read_status", _fake_read_status)

        service_mod._win_restart()

        out = capsys.readouterr().out
        assert "Restarted the task" in out
        assert "has not published fresh status" not in out

    def test_restart_timeout_reports_honestly(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """The pid never actually changes (same 111 before and after) --

        the "new" process never publishes a fresh write within the
        timeout, so this must time out honestly, never fabricate success.
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_RESTART_STATUS_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.005)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )
        monkeypatch.setattr(
            statusfile_mod, "read_status", lambda path=None: {"pid": 111}
        )

        service_mod._win_restart()  # must not raise

        out = capsys.readouterr().out
        assert "has not published" in out
        assert "fresh" in out
        assert "Restarted the task" not in out

    def test_restart_with_no_prior_status_accepts_any_fresh_pid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """No status file existed before this restart (e.g. the very first

        start) -- baseline_pid is None, so ANY freshly observed pid counts
        as fresh, matching `_win_wait_for_fresh_status()`'s documented
        contract.
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.001)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )

        calls = {"n": 0}

        def _fake_read_status(path: Path | None = None) -> dict[str, Any] | None:
            calls["n"] += 1
            return None if calls["n"] == 1 else {"pid": 999}

        monkeypatch.setattr(statusfile_mod, "read_status", _fake_read_status)

        service_mod._win_restart()

        out = capsys.readouterr().out
        assert "Restarted the task" in out

    def test_restart_run_failure_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["schtasks", "/Run"]:
                return _FakeCompletedProcess(1, stderr="ERROR: task not found")
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        with pytest.raises(SystemExit) as exc_info:
            service_mod._win_restart()
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "task not found" in out

    def test_restart_never_calls_service_main_pid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Regression guard: `_win_restart()` must use

        `_win_wait_for_fresh_status()`'s baseline-pid diff, never
        `service_main_pid()` -- which always returns None on Windows (see
        its docstring) and would make this always time out.
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.001)
        monkeypatch.setattr(
            service_mod.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(0)
        )

        def _boom() -> int | None:
            raise AssertionError("_win_restart must not call service_main_pid()")

        monkeypatch.setattr(service_mod, "service_main_pid", _boom)

        calls = {"n": 0}

        def _fake_read_status(path: Path | None = None) -> dict[str, Any]:
            calls["n"] += 1
            return {"pid": 111} if calls["n"] == 1 else {"pid": 999}

        monkeypatch.setattr(statusfile_mod, "read_status", _fake_read_status)

        service_mod._win_restart()

        out = capsys.readouterr().out
        assert "Restarted the task" in out

    def test_restart_does_not_issue_run_until_task_query_reports_stopped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Regression guard for the real-hardware race (state 3 after

        restart): `/Run` must not be issued until `_win_task_query()`
        (polled via `_win_wait_for_task_stopped()`) actually observes the
        task has stopped -- a bare "/Run right after /End" is exactly what
        let `IgnoreNew` silently drop the new run request.
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_WIN_STOP_POLL_INTERVAL_SECONDS", 0.001)
        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.001)

        # Baseline read (before `_win_stop()`) sees the OLD pid; every read
        # after that sees the NEW process's pid, so
        # `_win_wait_for_fresh_status()` can observe a genuine change once
        # `/Run` succeeds -- same pattern the other restart tests use.
        status_calls = {"n": 0}

        def _fake_read_status(path: Any = None) -> dict[str, Any]:
            status_calls["n"] += 1
            return {"pid": 111} if status_calls["n"] == 1 else {"pid": 999}

        monkeypatch.setattr(statusfile_mod, "read_status", _fake_read_status)

        call_order: list[str] = []
        query_calls = {"n": 0}

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            if argv[:2] == ["schtasks", "/End"]:
                call_order.append("end")
                return _FakeCompletedProcess(0)
            if argv[:2] == ["schtasks", "/Run"]:
                call_order.append("run")
                return _FakeCompletedProcess(0)
            if argv and argv[0] == "powershell.exe":
                query_calls["n"] += 1
                call_order.append("query")
                # RUNNING for the first two queries, stopped from then on.
                state = (
                    service_mod._WIN_TASK_STATE_RUNNING if query_calls["n"] < 3 else 3
                )
                return _FakeCompletedProcess(0, stdout=f"OK {state} 0\n")
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._win_restart()

        assert query_calls["n"] >= 3
        run_index = call_order.index("run")
        assert call_order[:run_index].count("query") >= 3
        assert "end" in call_order[:run_index]

        out = capsys.readouterr().out
        assert "Task did not report stopped" not in out
        assert "Restarted the task" in out

    def test_restart_still_attempts_run_when_task_never_reports_stopped(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """Never silently give up: an honest ACT check is added, but the

        restart is still attempted -- matching `_launchd_restart()`'s own
        behavior when `_wait_for_launchd_unload()` times out.
        """
        from muxplex_deck import statusfile as statusfile_mod

        monkeypatch.setattr(service_mod, "_WIN_STOP_POLL_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(service_mod, "_WIN_STOP_POLL_INTERVAL_SECONDS", 0.005)
        monkeypatch.setattr(service_mod, "_RESTART_STATUS_TIMEOUT_SECONDS", 0.02)
        monkeypatch.setattr(service_mod, "_RESTART_STATUS_POLL_INTERVAL_SECONDS", 0.005)
        monkeypatch.setattr(
            statusfile_mod, "read_status", lambda path=None: {"pid": 999}
        )

        calls: list[list[str]] = []

        def _fake_run(argv: list[str], **kwargs: Any) -> Any:
            calls.append(list(argv))
            if argv and argv[0] == "powershell.exe":
                return _FakeCompletedProcess(
                    0, stdout=f"OK {service_mod._WIN_TASK_STATE_RUNNING} 999\n"
                )
            return _FakeCompletedProcess(0)

        monkeypatch.setattr(service_mod.subprocess, "run", _fake_run)

        service_mod._win_restart()  # must not raise / must not exit nonzero

        out = capsys.readouterr().out
        assert "Task did not report stopped" in out
        assert any(c[:2] == ["schtasks", "/Run"] for c in calls)


class TestWinStatus:
    def test_registered_and_running_shows_pid(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(
                exists=True, state=service_mod._WIN_TASK_STATE_RUNNING, pid=1234
            ),
        )
        service_mod._win_status()
        out = capsys.readouterr().out
        assert "Registered and running" in out
        assert "1234" in out
        assert "Ready." in out

    def test_not_registered_shows_action(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=False, state=None, pid=None),
        )
        service_mod._win_status()
        out = capsys.readouterr().out
        assert "Not registered" in out
        assert "Do this:" in out
        assert "muxplex-deck service install" in out

    def test_registered_but_not_running_shows_action(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(exists=True, state=3, pid=None),
        )
        service_mod._win_status()
        out = capsys.readouterr().out
        assert "not running" in out
        assert "Do this:" in out

    def test_always_shows_xml_and_log_paths(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(
            service_mod,
            "_win_task_query",
            lambda: service_mod.WinTaskInfo(
                exists=True, state=service_mod._WIN_TASK_STATE_RUNNING, pid=1
            ),
        )
        service_mod._win_status()
        out = capsys.readouterr().out
        assert str(service_mod._win_task_xml_path()) in out
        assert str(service_mod._win_default_log_path()) in out


class TestWinLogs:
    def test_logs_streams_via_get_content_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorder = _RecordingRun()
        monkeypatch.setattr(service_mod.subprocess, "run", recorder)

        service_mod._win_logs()

        assert len(recorder.calls) == 1
        assert recorder.calls[0][0] == "powershell.exe"
        command = recorder.calls[0][-1]
        assert "Get-Content" in command
        assert "-Tail 50" in command
        assert "-Wait" in command

    def test_logs_swallows_keyboard_interrupt(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a: Any, **k: Any) -> Any:
            raise KeyboardInterrupt()

        monkeypatch.setattr(service_mod.subprocess, "run", _raise)
        service_mod._win_logs()  # must not raise


# ---------------------------------------------------------------------------
# Dispatch -- darwin -> windows -> systemd -> unsupported ordering
# (WINDOWS_NATIVE_SPEC.md section 1.6).
# ---------------------------------------------------------------------------


class TestWindowsDispatch:
    def _mock_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: False)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        monkeypatch.setattr(service_mod, "_have_systemctl", lambda: False)

    def test_service_install_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_install", lambda: calls.append(True))
        service_mod.service_install()
        assert calls == [True]

    def test_service_uninstall_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_uninstall", lambda: calls.append(True))
        service_mod.service_uninstall()
        assert calls == [True]

    def test_service_start_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_start", lambda: calls.append(True))
        service_mod.service_start()
        assert calls == [True]

    def test_service_stop_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_stop", lambda: calls.append(True))
        service_mod.service_stop()
        assert calls == [True]

    def test_service_restart_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_restart", lambda: calls.append(True))
        service_mod.service_restart()
        assert calls == [True]

    def test_service_status_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_status", lambda: calls.append(True))
        service_mod.service_status()
        assert calls == [True]

    def test_service_logs_dispatches_to_win(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._mock_win(monkeypatch)
        calls: list[bool] = []
        monkeypatch.setattr(service_mod, "_win_logs", lambda: calls.append(True))
        service_mod.service_logs()
        assert calls == [True]

    def test_darwin_wins_over_windows_when_somehow_both_true(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Dispatch order is darwin -> windows -> systemd (WINDOWS_NATIVE_SPEC.md

        section 1.6) -- pins the ordering even though this combination
        can't occur on a real machine.
        """
        monkeypatch.setattr(service_mod, "_is_darwin", lambda: True)
        monkeypatch.setattr(service_mod, "_is_windows", lambda: True)
        calls: list[str] = []
        monkeypatch.setattr(
            service_mod, "_launchd_install", lambda: calls.append("darwin")
        )
        monkeypatch.setattr(service_mod, "_win_install", lambda: calls.append("win"))
        service_mod.service_install()
        assert calls == ["darwin"]


# ---------------------------------------------------------------------------
# _win_user_id -- best-effort DOMAIN\username, no stored password required.
# ---------------------------------------------------------------------------


class TestWinUserId:
    def test_uses_domain_and_username_when_both_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("USERDOMAIN", "DESKTOP-X")
        monkeypatch.setenv("USERNAME", "bob")
        assert service_mod._win_user_id() == r"DESKTOP-X\bob"

    def test_bare_username_when_no_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("USERDOMAIN", raising=False)
        monkeypatch.setenv("USERNAME", "bob")
        assert service_mod._win_user_id() == "bob"

    def test_falls_back_to_getpass_when_username_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("USERDOMAIN", raising=False)
        monkeypatch.delenv("USERNAME", raising=False)
        monkeypatch.setattr(service_mod.getpass, "getuser", lambda: "fallback-user")
        assert service_mod._win_user_id() == "fallback-user"


# ---------------------------------------------------------------------------
# _win_default_log_path / _win_task_xml_path -- one state directory,
# alongside status.json (WINDOWS_NATIVE_SPEC.md section 1.5 / 3.1).
# ---------------------------------------------------------------------------


class TestWinPaths:
    def test_log_path_is_alongside_status_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from muxplex_deck import statusfile

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert (
            service_mod._win_default_log_path().parent
            == statusfile.default_status_path().parent
        )
        assert service_mod._win_default_log_path().name == "muxplex-deck.log"

    def test_xml_path_is_alongside_status_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from muxplex_deck import statusfile

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert (
            service_mod._win_task_xml_path().parent
            == statusfile.default_status_path().parent
        )
