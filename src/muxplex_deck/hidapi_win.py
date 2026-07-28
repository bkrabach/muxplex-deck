"""Windows-only: make the vendored `hidapi.dll` resolvable by streamdeck's loader.

`StreamDeck/Transport/LibUSBHIDAPI.py` probes for a native HIDAPI shared
library by name via ctypes -- Windows ships no such library, so without
this module a Windows user gets `ProbeError: ... Is the 'hidapi.dll'
library installed?` (see WINDOWS_NATIVE_SPEC.md section 2).

**The load-path trap this module exists to close** (verified by reading
CPython + streamdeck source, section 2.1 of the spec):

    for lib_name in library_search_list:                      # streamdeck
        found_lib = ctypes.util.find_library(name_no_ext)      # <-- FIRST
        HIDAPI_INSTANCE = ctypes.cdll.LoadLibrary(found_lib or lib_name)

`ctypes.util.find_library()` on Windows (the `nt` branch) searches
**`%PATH%` only** and returns the first match. `ctypes.CDLL`'s own loader
(reached when `find_library` returns `None`) uses
`LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`, which honors `os.add_dll_directory()`
but does **not** consult `%PATH%`. Net effect: a stray or wrong-arch
`hidapi.dll` earlier on `%PATH%` wins over anything registered via
`add_dll_directory`, `LoadLibrary` on it fails, the bare `except: pass` in
streamdeck's probe swallows the failure, and the vendored DLL is never
tried at all. This is a silent failure mode and both mechanisms below are
required to close it -- neither one alone is sufficient.

No-op (returns `None`, touches nothing) on every non-Windows platform.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["ensure_hidapi", "resolved_library_path", "vendored_dll_path"]

# Holds the `os.add_dll_directory()` return value. That object's __del__
# un-registers the directory when garbage collected -- keeping it at module
# scope for the life of the process is what makes the registration stick.
_dll_directory_cookie: object | None = None


def vendored_dll_path() -> Path:
    """Path where the vendored `x64/hidapi.dll` is expected, whether or not it exists.

    A plain path computation, not an existence check -- callers that need
    to know if it's actually there should check `.exists()` themselves or
    call `ensure_hidapi()`, which does.
    """
    return Path(__file__).parent / "_vendor" / "hidapi" / "x64" / "hidapi.dll"


def ensure_hidapi() -> Path | None:
    """Register the vendored HIDAPI directory so streamdeck's loader finds it.

    No-op on non-Windows (returns `None` immediately, no filesystem or
    environment changes). On `win32`:

    - Returns `None` if the vendored DLL is absent (arm64 Windows, or a
      source checkout that doesn't carry the binary blob) -- callers fall
      back to guidance (see `hidhelp._windows_guidance`'s `WIN-NOHIDAPI`);
      this function never raises.
    - Otherwise registers the DLL's directory via `os.add_dll_directory()`
      *and* prepends it to `%PATH%` (both are required -- see the module
      docstring), then returns the directory.

    Idempotent: safe to call from every entry point (`RealDeviceManager`,
    `deck-probe`) without worrying about double-registration -- the
    `add_dll_directory` cookie is only created once, and the `%PATH%`
    prepend is skipped if our directory is already present.
    """
    global _dll_directory_cookie

    if sys.platform != "win32":
        return None

    dll_path = vendored_dll_path()
    if not dll_path.exists():
        return None

    dll_dir = dll_path.parent

    if _dll_directory_cookie is None:
        _dll_directory_cookie = os.add_dll_directory(str(dll_dir))  # type: ignore[attr-defined]

    dir_str = str(dll_dir)
    current_path = os.environ.get("PATH", "")
    existing_entries = current_path.split(os.pathsep) if current_path else []
    if dir_str not in existing_entries:
        os.environ["PATH"] = (
            f"{dir_str}{os.pathsep}{current_path}" if current_path else dir_str
        )

    return dll_dir


def resolved_library_path() -> str | None:
    """What `ctypes.util.find_library("hidapi")` actually resolves to, right now.

    `None` on non-Windows (nothing to resolve -- other platforms use their
    own dynamic linker conventions) or if nothing on `%PATH%` matches.
    Does **not** call `ensure_hidapi()` itself -- callers that want "as
    streamdeck will actually see it" must call `ensure_hidapi()` first, so
    the comparison reflects the same environment streamdeck's probe runs
    in. This split (rather than folding the call in here) keeps this
    function a pure read of current process state, easy to test without
    also exercising the registration side effects.
    """
    if sys.platform != "win32":
        return None
    import ctypes.util

    return ctypes.util.find_library("hidapi")
