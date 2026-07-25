"""Device seam: the `DeckDevice` protocol the sidecar depends on.

`main.py` talks to a Stream Deck+ only through this protocol -- it never
imports `StreamDeck.DeviceManager` or constructs a device itself. Two
backends implement it:

- `device_real.py` -- the actual hardware, via the `streamdeck` library.
- `emulator.py`     -- an in-process virtual deck driven by a localhost
                       HTTP UI, for development/testing with no hardware.

Importing this module -- including the `DialEventType` / `TouchscreenEventType`
enums re-exported below -- never touches hidapi. Only *constructing* a real
backend's manager does that (see `device_real.RealDeviceManager`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from StreamDeck.Devices.StreamDeck import DialEventType, TouchscreenEventType

__all__ = [
    "DeckDevice",
    "DeviceManager",
    "DeviceProbeError",
    "DialCallback",
    "DialEventType",
    "KeyCallback",
    "TouchCallback",
    "TouchscreenEventType",
]


class DeviceProbeError(Exception):
    """Raised when a backend cannot be constructed (e.g. hidapi missing).

    The message is pre-formatted, ready to print to stderr as-is.
    """


# Callback shapes match the `streamdeck` library's own callback signatures
# exactly (see StreamDeck.Devices.StreamDeck.KeyCallback / DialCallback /
# TouchScreenCallback), so both backends can be handed to the same code.
KeyCallback = Callable[["DeckDevice", int, bool], None]
DialCallback = Callable[["DeckDevice", int, DialEventType, object], None]
TouchCallback = Callable[["DeckDevice", TouchscreenEventType, dict], None]


@runtime_checkable
class DeckDevice(Protocol):
    """Exactly the Stream Deck+ surface `muxplex_deck.main` uses.

    Both `device_real.RealDeckDevice` (wrapping the physical device) and
    `emulator.EmulatorDevice` (the virtual one) implement this in full.
    """

    def open(self) -> None: ...
    def close(self) -> None: ...
    def reset(self) -> None: ...
    def is_open(self) -> bool: ...
    def connected(self) -> bool: ...

    def key_count(self) -> int: ...
    def key_layout(self) -> tuple[int, int]: ...
    def dial_count(self) -> int: ...
    def is_touch(self) -> bool: ...
    def touch_key_count(self) -> int: ...
    def is_visual(self) -> bool: ...
    def vendor_id(self) -> int: ...
    def product_id(self) -> int: ...
    def deck_type(self) -> str: ...
    def get_serial_number(self) -> str: ...
    def get_firmware_version(self) -> str: ...

    def key_image_format(self) -> dict: ...
    def touchscreen_image_format(self) -> dict: ...
    def set_brightness(self, percent: float) -> None: ...
    def set_key_image(self, key: int, image: bytes) -> None: ...
    def set_touchscreen_image(
        self,
        image: bytes,
        x_pos: int = 0,
        y_pos: int = 0,
        width: int = 0,
        height: int = 0,
    ) -> None: ...

    def set_key_callback(self, callback: KeyCallback | None) -> None: ...
    def set_dial_callback(self, callback: DialCallback | None) -> None: ...
    def set_touchscreen_callback(self, callback: TouchCallback | None) -> None: ...

    def __enter__(self) -> None: ...
    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None: ...


@runtime_checkable
class DeviceManager(Protocol):
    """Backend-level 'find a device' seam -- the virtual or real USB bus."""

    def find_device(self) -> DeckDevice | None: ...
