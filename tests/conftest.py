"""Safety rails for the muxplex-deck test suite.

WHY THIS FILE EXISTS -- do not delete it, and do not weaken the guards:

muxplex-deck's sibling repo, muxplex, had its test suite cause REAL production
damage twice in one day (2026-07-25): a test overwrote the developer's real
``~/.config/muxplex/settings.json`` with fixture data, and six tests called
the real ``serve()`` and SIGTERMed the developer's live, running server.
Neither was noticed for hours, because from inside the suite everything
looked green -- a test that damages its host still passes.

muxplex-deck has NOT (as of this writing) had an incident like that -- this
file is PREVENTIVE, modeled directly on muxplex's real one, because
muxplex-deck has the exact same exposure class:

  - it reads/writes ``~/.config/muxplex-deck/config.json`` and a
    ``federation_key`` secret file (mode 0600)
  - it writes a status file under ``$XDG_STATE_HOME/muxplex-deck/status.json``
  - it manages a REAL systemd (Linux) / launchd (macOS) user service:
    install, uninstall, start, stop, restart -- by shelling out to
    ``systemctl``/``launchctl``/``loginctl``
  - it opens EXCLUSIVE HID access to a real Stream Deck

A careless future test could uninstall a real installed service, clobber a
real config file, leak or overwrite the federation key secret, or grab the
HID device out from under a running sidecar. None of that would show up as a
test failure -- exactly the muxplex failure mode.

The rails below make each of those hazards structurally unreachable BY
DEFAULT. A marker-gated escape hatch exists for the rare test that
genuinely needs the real thing, so the opt-in is explicit and visible to a
reviewer. ``test_safety_rails.py`` fails loudly if any rail is removed,
renamed, or quietly weakened -- a future session with none of this context
still gets stopped.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Session guard: refuse to run if a REAL muxplex-deck service is active
# ---------------------------------------------------------------------------

_SESSION_OVERRIDE_ENV = "MUXPLEX_DECK_TEST_ALLOW_LIVE_SERVICE"


def pytest_sessionstart(session: pytest.Session) -> None:
    """Refuse to run when a real, active muxplex-deck service could be harmed.

    This runs before any fixture (including the autouse isolation rails
    below), so it is a genuine read of the live host -- exactly like probing
    a real port before any test has a chance to touch it. The autouse rails
    make every default path/subprocess/HID access safe by default, but a
    test opting in via ``@pytest.mark.allow_real_subprocess`` or
    ``@pytest.mark.allow_real_hid`` could still reach a real, currently
    active service. This is the belt-and-suspenders check for that case.
    """
    if os.environ.get(_SESSION_OVERRIDE_ENV) == "1":
        return
    try:
        from muxplex_deck.service import service_is_active
    except Exception:  # pragma: no cover - import shape changed
        return
    try:
        active = service_is_active()
    except Exception:  # pragma: no cover - never let the guard crash the run
        return
    if not active:
        return

    raise pytest.UsageError(
        "\n"
        "REFUSING TO RUN: a real muxplex-deck service is currently active on\n"
        "this host.\n"
        "\n"
        "This suite manages systemd/launchd services and real HID devices.\n"
        "The autouse isolation rails in tests/conftest.py redirect every\n"
        "default config/service/status path and neutralize subprocess + HID\n"
        "access, but a test marked @pytest.mark.allow_real_subprocess or\n"
        "@pytest.mark.allow_real_hid could still reach the real service.\n"
        "\n"
        "Run the suite in an isolated environment instead -- see AGENTS.md,\n"
        "'Testing'.\n"
        "\n"
        "If you are certain nothing live is at risk (fresh container, CI\n"
        "runner, no muxplex-deck service on this host), override explicitly:\n"
        "\n"
        f"    {_SESSION_OVERRIDE_ENV}=1 pytest\n"
    )


# ---------------------------------------------------------------------------
# Rail 1: config.json / federation_key -- default path isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_config_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the DEFAULT config resolution at a per-test tmp file.

    ``config._resolve_config_path()`` checks an explicit path, then the
    ``MUXPLEX_DECK_CONFIG`` env var, then falls back to
    ``~/.config/muxplex-deck/config.json`` via the sudo-aware
    ``_invoking_user_home()``. Individual tests either pass an explicit
    ``config_path`` (most do) or deliberately fake ``SUDO_USER``/``pwd`` to
    exercise that exact sudo-resolution logic (see ``test_config.py``) --
    so this rail does NOT touch ``_invoking_user_home`` itself, which would
    break those dedicated tests. Instead it sets the env var that
    ``_resolve_config_path`` checks BEFORE ever reaching the sudo logic,
    closing the gap for a test that calls ``load_config(None)`` (or
    ``load_raw_config``/``save_raw_config``/``patch_raw_config``/
    ``check_config_file``) and forgets to pass or fake anything: it lands on
    a fresh, empty tmp file instead of the developer's real config -- and
    since ``load_config`` raises before reaching key-file logic when the
    file doesn't exist, the real ``federation_key`` is never touched either.
    The two tests that need the true default-resolution path already delete
    this env var themselves (``test_config.py``'s sudo tests).
    """
    monkeypatch.setenv("MUXPLEX_DECK_CONFIG", str(tmp_path / "default-config.json"))
    yield


# ---------------------------------------------------------------------------
# Rail 2: systemd/launchd unit + plist paths -- never write to the real ones
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_service_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect every systemd/launchd path constant under a per-test tmp dir.

    ``service.py`` computes ``_SYSTEMD_UNIT_DIR``/``_SYSTEMD_UNIT_PATH``/
    ``_LAUNCHD_PLIST_DIR``/``_LAUNCHD_PLIST_PATH`` from ``Path.home()`` at
    import time. A test that calls ``service_install()``/
    ``_systemd_install()``/etc. without redirecting these (many tests do
    explicitly; this is the backstop for one that forgets) would write a
    REAL unit file into ``~/.config/systemd/user/`` or a REAL plist into
    ``~/Library/LaunchAgents/`` -- and if a real muxplex-deck unit is
    already installed there, ``service_uninstall()`` would stop, disable,
    and delete it.
    """
    try:
        import muxplex_deck.service as service_mod
    except Exception:  # pragma: no cover - import shape changed
        yield
        return
    systemd_dir = tmp_path / "systemd-user"
    launchd_dir = tmp_path / "launchagents"
    monkeypatch.setattr(service_mod, "_SYSTEMD_UNIT_DIR", systemd_dir, raising=False)
    monkeypatch.setattr(
        service_mod,
        "_SYSTEMD_UNIT_PATH",
        systemd_dir / "muxplex-deck.service",
        raising=False,
    )
    monkeypatch.setattr(service_mod, "_LAUNCHD_PLIST_DIR", launchd_dir, raising=False)
    monkeypatch.setattr(
        service_mod,
        "_LAUNCHD_PLIST_PATH",
        launchd_dir / "com.muxplex-deck.plist",
        raising=False,
    )
    yield


# ---------------------------------------------------------------------------
# Rail 3: status.json -- never write into the real $XDG_STATE_HOME
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_status_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the status-file location under a per-test tmp dir.

    Never contains secrets (see ``statusfile.py``'s own docstring), but
    writing fixture data over a REAL running sidecar's status file would
    corrupt what ``muxplex-deck status`` reports for that real process, and
    a test reading a stale REAL file could assert on a stranger's hardware.

    Sets ``XDG_STATE_HOME`` rather than stubbing ``default_status_dir``/
    ``default_status_path`` directly, for the same reason Rail 1 sets
    ``MUXPLEX_DECK_CONFIG`` rather than patching ``_invoking_user_home``:
    ``test_statusfile.py`` has dedicated tests of ``default_status_dir``'s
    own ``XDG_STATE_HOME``-vs-``~/.local/state`` fallback logic, and
    stubbing the function itself would silently stop testing what they
    claim to test. Those tests set/delete ``XDG_STATE_HOME`` themselves,
    which -- because it happens in the test body, after this fixture's
    setup -- simply overrides this default.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    yield


# ---------------------------------------------------------------------------
# Rail 4: subprocess.run -- never shell out to a real systemctl/launchctl/
# loginctl/openssl/git/uv/pip from a test that forgot to mock it
# ---------------------------------------------------------------------------


def _blocked_subprocess_run(*args: object, **kwargs: object):
    raise AssertionError(
        "REFUSING TO RUN A REAL subprocess.run() FROM A TEST.\n"
        "\n"
        "This suite manages a real systemd/launchd service and calls out to\n"
        "systemctl/launchctl/loginctl/openssl/git/uv/pip. A test that reaches\n"
        "the real subprocess.run without mocking it would run those commands\n"
        "for real on THIS host -- including against a real installed\n"
        "muxplex-deck service.\n"
        "\n"
        "Mock subprocess.run yourself (see test_cli_service.py's\n"
        "`recording_run` fixture for the pattern), or if this test genuinely\n"
        "needs the real thing, opt in explicitly:\n"
        "\n"
        "    @pytest.mark.allow_real_subprocess\n"
    )


@pytest.fixture(autouse=True)
def _neutralize_subprocess(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """No test may invoke the REAL ``subprocess.run`` by accident.

    Patches the actual ``subprocess`` module's ``run`` attribute -- every
    module in this codebase that does ``import subprocess;
    subprocess.run(...)`` (service.py, cli.py, focus.py) shares this one
    attribute, so this single patch closes the whole class rather than one
    function in one module. Tests that legitimately need the real thing opt
    in explicitly:

        @pytest.mark.allow_real_subprocess

    A future test author who forgets is stopped with a clear message. One
    who needs the real thing has to say so in a way a reviewer can see.
    """
    if "allow_real_subprocess" in request.keywords:
        yield
        return
    monkeypatch.setattr(subprocess, "run", _blocked_subprocess_run)
    yield


# ---------------------------------------------------------------------------
# Rail 5: real HID device access -- never open/enumerate a real Stream Deck
# ---------------------------------------------------------------------------


class _NullRealDeviceManager:
    """Stub swapped in for ``device_real.RealDeviceManager`` by default.

    Never touches hidapi and never enumerates the real USB bus;
    ``find_device`` always reports "no device attached" rather than reaching
    for the exclusive real Stream Deck handle. Tests that need a specific
    fake device already provide their own monkeypatch of
    ``RealDeviceManager`` (most do, e.g. test_cli_doctor.py's
    ``_FakeManager``) -- since that patch happens in the test body, it
    simply overrides this one. Only a test that wants the ACTUAL real class
    needs to opt in.
    """

    def __init__(self) -> None:
        pass

    def find_device(self):
        return None


@pytest.fixture(autouse=True)
def _neutralize_real_hid(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """No test may construct the REAL ``device_real.RealDeviceManager``.

    Both ``main._build_manager`` and ``cli.py``'s hardware checks do a lazy,
    per-call ``from .device_real import RealDeviceManager`` rather than a
    module-level import, so patching the attribute on ``device_real`` itself
    is picked up on every call. Tests that legitimately need real hardware
    opt in explicitly:

        @pytest.mark.allow_real_hid
    """
    if "allow_real_hid" in request.keywords:
        yield
        return
    try:
        import muxplex_deck.device_real as device_real_mod
    except Exception:  # pragma: no cover - import shape changed
        yield
        return
    monkeypatch.setattr(
        device_real_mod, "RealDeviceManager", _NullRealDeviceManager, raising=False
    )
    yield
