"""Meta-tests: the safety rails must not be silently removable.

``conftest.py`` documents why these rails exist -- muxplex-deck shares its
exposure class (real config/secret files, a real systemd/launchd service, an
exclusive real HID device) with its sibling repo muxplex, whose suite caused
real production damage twice in one day. These rails are PREVENTIVE for
muxplex-deck, modeled on that real incident. They only help if they survive
contact with future contributors who have none of this context. These tests
fail loudly if a rail is deleted, renamed, or quietly weakened.

If you are here because one of these failed: read ``conftest.py`` first. The
rails are not ceremony -- each one maps to a real hazard this codebase has.
"""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
from pathlib import Path

import pytest

_CONFTEST_PATH = Path(__file__).parent / "conftest.py"

# Capture the ORIGINAL, unpatched references at module-collection time --
# before any per-test autouse fixture has had a chance to run. Pytest always
# finishes importing conftest.py and every test module (module-level code
# only) before it starts setting up fixtures for the first test, so these are
# guaranteed to be the real, unmodified objects.
_ORIGINAL_SUBPROCESS_RUN = subprocess.run

try:
    import muxplex_deck.device_real as _device_real_mod

    _ORIGINAL_REAL_DEVICE_MANAGER: object | None = _device_real_mod.RealDeviceManager
except Exception:  # noqa: BLE001 -- pragma: no cover - import shape changed
    _device_real_mod = None  # type: ignore[assignment]
    _ORIGINAL_REAL_DEVICE_MANAGER = None


def _load_conftest():
    """Load conftest.py as a standalone module, independent of pytest's
    import mode / package layout, so these meta-tests work regardless of
    whether ``tests/`` is a package.
    """
    spec = importlib.util.spec_from_file_location(
        "muxplex_deck_tests_conftest_meta", _CONFTEST_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Rail 0: the file itself, and every rail function, must be present
# ---------------------------------------------------------------------------


def test_conftest_exists():
    """The safety-rail file itself must be present."""
    assert _CONFTEST_PATH.is_file(), (
        "tests/conftest.py is missing. It holds the guards that stop this "
        "suite from touching a real config file, federation key, systemd/ "
        "launchd service, or HID device. Restore it."
    )


def test_all_rails_present():
    ct = _load_conftest()
    required = [
        "pytest_sessionstart",
        "_isolate_config_default_path",
        "_isolate_service_paths",
        "_isolate_status_path",
        "_neutralize_subprocess",
        "_neutralize_real_hid",
    ]
    missing = [name for name in required if not hasattr(ct, name)]
    assert not missing, (
        f"conftest.py is missing rail(s): {missing}. Each one closes a "
        f"specific hazard documented in conftest.py's module docstring -- "
        f"restore it rather than deleting it."
    )


def test_session_guard_aborts_not_warns():
    """``pytest_sessionstart`` must refuse to run, not merely print a warning."""
    ct = _load_conftest()
    src = inspect.getsource(ct.pytest_sessionstart)
    assert "UsageError" in src, (
        "pytest_sessionstart no longer aborts. It must FAIL, not warn -- a "
        "warning gets scrolled past and a real service gets torn down anyway."
    )


def test_session_override_requires_explicit_optin():
    ct = _load_conftest()
    assert ct._SESSION_OVERRIDE_ENV == "MUXPLEX_DECK_TEST_ALLOW_LIVE_SERVICE"
    src = inspect.getsource(ct.pytest_sessionstart)
    assert '== "1"' in src, "override must require an exact opt-in value"


# ---------------------------------------------------------------------------
# Rail 1: config.json / federation_key default-path isolation
# ---------------------------------------------------------------------------


def test_config_default_path_is_isolated():
    """A test that calls ``load_config``/``load_raw_config`` with no explicit
    path must land in tmp, never under the developer's real home.
    """
    import muxplex_deck.config as config_mod

    resolved = config_mod._resolve_config_path(None)
    real_home = Path.home()
    assert not str(resolved).startswith(str(real_home)), (
        f"Default config path resolved to {resolved}, under the real home "
        f"({real_home}). A test forgetting to pass config_path explicitly "
        f"would read/write the developer's real muxplex-deck config.json "
        f"and, transitively, its federation_key."
    )


def test_config_isolation_does_not_break_sudo_resolution_tests():
    """Regression guard for the rail's own design constraint: it must NOT
    monkeypatch ``_invoking_user_home`` itself, or test_config.py's dedicated
    sudo-resolution tests (which fake SUDO_USER/pwd and call ``_expand``
    directly) would silently stop testing what they claim to test.
    """
    ct = _load_conftest()
    src = inspect.getsource(ct._isolate_config_default_path)
    assert 'setattr(config_mod, "_invoking_user_home"' not in src, (
        "_isolate_config_default_path must not monkeypatch "
        "_invoking_user_home -- doing so breaks test_config.py's dedicated "
        "sudo-resolution tests, which rely on that function's real "
        "behavior. Use the MUXPLEX_DECK_CONFIG env var instead."
    )
    assert "MUXPLEX_DECK_CONFIG" in src


# ---------------------------------------------------------------------------
# Rail 2: systemd/launchd path isolation
# ---------------------------------------------------------------------------


def test_service_paths_isolated_by_default():
    import muxplex_deck.service as service_mod

    real_home = Path.home()
    assert not str(service_mod._SYSTEMD_UNIT_PATH).startswith(
        str(real_home / ".config" / "systemd")
    ), (
        f"_SYSTEMD_UNIT_PATH ({service_mod._SYSTEMD_UNIT_PATH}) points at the "
        f"real systemd user directory. A test that forgets to redirect it "
        f"would write/remove a REAL unit file."
    )
    assert not str(service_mod._LAUNCHD_PLIST_PATH).startswith(
        str(real_home / "Library" / "LaunchAgents")
    ), (
        f"_LAUNCHD_PLIST_PATH ({service_mod._LAUNCHD_PLIST_PATH}) points at "
        f"the real LaunchAgents directory. A test that forgets to redirect "
        f"it would write/remove a REAL plist file."
    )


# ---------------------------------------------------------------------------
# Rail 3: status.json path isolation
# ---------------------------------------------------------------------------


def test_status_path_isolated_by_default():
    import muxplex_deck.statusfile as statusfile_mod

    real_home = Path.home()
    resolved = statusfile_mod.default_status_path()
    assert not str(resolved).startswith(str(real_home)), (
        f"default_status_path() resolved to {resolved}, under the real home "
        f"({real_home}). A test that forgets to redirect it would overwrite "
        f"a real running sidecar's published status."
    )


# ---------------------------------------------------------------------------
# Rail 4: subprocess.run neutralization
# ---------------------------------------------------------------------------


def test_subprocess_run_is_neutralized_by_default():
    """A test that does not opt in must not reach the real subprocess.run."""
    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        subprocess.run(["true"], check=False)


@pytest.mark.allow_real_subprocess
def test_optin_marker_restores_real_subprocess():
    """The opt-in marker must hand back the actual subprocess.run -- proven
    by invoking a real, harmless command (``true``, a no-op on every POSIX
    platform).
    """
    result = subprocess.run(["true"], check=False)
    assert result.returncode == 0
    assert subprocess.run is _ORIGINAL_SUBPROCESS_RUN


def test_subprocess_override_requires_explicit_marker():
    ct = _load_conftest()
    src = inspect.getsource(ct._neutralize_subprocess)
    assert "allow_real_subprocess" in src


def test_wsl_list_devices_is_neutralized_by_default():
    """`wsl.list_devices` must go through the same blocked `subprocess.run`
    as everything else -- a careless test that forgets to mock it must not
    reach a real `usbipd.exe` and enumerate a real Windows USB attachment.
    """
    import muxplex_deck.wsl as wsl_mod

    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        wsl_mod.list_devices(Path("/fake/usbipd.exe"))


def test_wsl_attach_is_neutralized_by_default():
    """`wsl.attach` -- the ONE mutating function in the WSL surface -- must
    go through the same blocked `subprocess.run`. Without this, a careless
    test could attach a real USB device away from a real Windows host.
    """
    import muxplex_deck.wsl as wsl_mod

    with pytest.raises(AssertionError, match="REFUSING TO RUN"):
        wsl_mod.attach(Path("/fake/usbipd.exe"), "1-4")


# ---------------------------------------------------------------------------
# Rail 5: real HID device neutralization
# ---------------------------------------------------------------------------


def test_hid_is_neutralized_by_default():
    import muxplex_deck.device_real as device_real_mod

    assert device_real_mod.RealDeviceManager is not _ORIGINAL_REAL_DEVICE_MANAGER, (
        "device_real.RealDeviceManager is not neutralized. Without the "
        "autouse fixture, any test that constructs it reaches the real "
        "hidapi library and enumerates a real USB Stream Deck."
    )
    mgr = device_real_mod.RealDeviceManager()
    assert mgr.find_device() is None


@pytest.mark.allow_real_hid
def test_optin_marker_restores_real_hid_class():
    import muxplex_deck.device_real as device_real_mod

    assert device_real_mod.RealDeviceManager is _ORIGINAL_REAL_DEVICE_MANAGER


def test_hid_override_requires_explicit_marker():
    ct = _load_conftest()
    src = inspect.getsource(ct._neutralize_real_hid)
    assert "allow_real_hid" in src
