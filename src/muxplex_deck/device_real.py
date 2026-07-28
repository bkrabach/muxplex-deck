"""Real Stream Deck+ backend: thin wrapper over the `streamdeck` library.

All hidapi-touching imports (`StreamDeck.DeviceManager`, `TransportError`)
are isolated in this module. `main.py` never imports them and never
constructs a `DeviceManager` itself -- that only happens here, behind
`RealDeviceManager`, so the emulator code path can run with hidapi absent.

`RealDeckDevice` wraps the library's `StreamDeck` instance and delegates
every call straight through, except `reset()`/`close()`, which swallow
`TransportError` -- the expected error when the cable is pulled mid-call --
exactly as `main.py`'s `_safe_close` did before this refactor. Everything
else (behavior, timing, callback shapes) is unchanged.
"""

from __future__ import annotations

from StreamDeck.DeviceManager import DeviceManager as _LibDeviceManager
from StreamDeck.DeviceManager import ProbeError as _LibProbeError
from StreamDeck.Devices.StreamDeck import StreamDeck as _LibStreamDeck
from StreamDeck.Transport.Transport import TransportError

from . import hidapi_win
from .device import (
    DeckDevice,
    DeviceProbeError,
    DialCallback,
    KeyCallback,
    TouchCallback,
)

__all__ = ["RealDeckDevice", "RealDeviceManager"]

_MISSING_HIDAPI_MESSAGE = (
    "Could not find the native HIDAPI library required to talk to the Stream Deck.\n"
    "Install it for your platform, then try again:\n"
    "  macOS:          brew install hidapi\n"
    "  Debian/Ubuntu:  sudo apt install libhidapi-libusb0\n"
    "  Windows:        muxplex-deck bundles hidapi.dll -- if you're seeing this,\n"
    "                  either the vendored copy is missing from this install\n"
    "                  (arm64 Windows, or a source checkout without the binary),\n"
    "                  or a different hidapi.dll earlier on %PATH% shadowed it.\n"
    "                  Run 'muxplex-deck doctor' for which DLL actually resolved,\n"
    "                  or get one yourself: https://github.com/libusb/hidapi/releases\n"
    "                  (the loader also honors ./hidapi.dll in the current directory).\n"
)


class RealDeckDevice:
    """Wraps a real `StreamDeck` instance to satisfy the `DeckDevice` protocol."""

    def __init__(self, deck: _LibStreamDeck) -> None:
        self._deck = deck

    def open(self) -> None:
        self._deck.open()

    def close(self) -> None:
        try:
            self._deck.close()
        except TransportError:
            pass

    def reset(self) -> None:
        try:
            if self._deck.is_open():
                self._deck.reset()
        except TransportError:
            pass

    def is_open(self) -> bool:
        return self._deck.is_open()

    def connected(self) -> bool:
        return self._deck.connected()

    def key_count(self) -> int:
        return self._deck.key_count()

    def key_layout(self) -> tuple[int, int]:
        return self._deck.key_layout()

    def dial_count(self) -> int:
        return self._deck.dial_count()

    def is_touch(self) -> bool:
        return self._deck.is_touch()

    def touch_key_count(self) -> int:
        return self._deck.touch_key_count()

    def is_visual(self) -> bool:
        return self._deck.is_visual()

    def vendor_id(self) -> int:
        return self._deck.vendor_id()

    def product_id(self) -> int:
        return self._deck.product_id()

    def deck_type(self) -> str:
        return self._deck.deck_type()

    def get_serial_number(self) -> str:
        return self._deck.get_serial_number()

    def get_firmware_version(self) -> str:
        return self._deck.get_firmware_version()

    def key_image_format(self) -> dict:
        return self._deck.key_image_format()

    def touchscreen_image_format(self) -> dict:
        return self._deck.touchscreen_image_format()

    def set_brightness(self, percent: float) -> None:
        self._deck.set_brightness(percent)

    def set_key_image(self, key: int, image: bytes) -> None:
        self._deck.set_key_image(key, image)

    def set_touchscreen_image(
        self,
        image: bytes,
        x_pos: int = 0,
        y_pos: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None:
        self._deck.set_touchscreen_image(image, x_pos, y_pos, width, height)

    def set_key_callback(self, callback: KeyCallback | None) -> None:
        self._deck.set_key_callback(callback)

    def set_dial_callback(self, callback: DialCallback | None) -> None:
        self._deck.set_dial_callback(callback)

    def set_touchscreen_callback(self, callback: TouchCallback | None) -> None:
        self._deck.set_touchscreen_callback(callback)

    def __enter__(self) -> None:
        self._deck.__enter__()

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self._deck.__exit__(exc_type, exc_val, exc_tb)


class RealDeviceManager:
    """Wraps `StreamDeck.DeviceManager.DeviceManager` -- the real USB bus.

    Construction is what probes hidapi; a missing library surfaces as
    `DeviceProbeError` with an actionable message rather than the library's
    own `ProbeError`.
    """

    def __init__(self) -> None:
        # No-op on non-Windows; on Windows this must run BEFORE
        # _LibDeviceManager() ever imports/loads the native library -- see
        # hidapi_win's module docstring for why both of its mechanisms are
        # required to make the vendored DLL win the load-path race.
        hidapi_win.ensure_hidapi()
        try:
            self._manager = _LibDeviceManager()
        except _LibProbeError as exc:
            raise DeviceProbeError(_MISSING_HIDAPI_MESSAGE) from exc

    def find_device(self) -> DeckDevice | None:
        decks = self._manager.enumerate()
        return RealDeckDevice(decks[0]) if decks else None
