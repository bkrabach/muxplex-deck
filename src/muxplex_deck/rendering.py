"""Pillow-based rendering for the muxplex sidecar.

Adapted from `deck_probe/rendering.py`'s proven patterns (native image
conversion via `PILHelper`, explicit touch-strip region size) but drawing
session names and server status instead of probe diagnostics. No device
I/O happens here -- these are pure image generators, kept separate from
`main.py`'s state machine so rendering stays testable in isolation.

Functions here accept the `DeckDevice` protocol (real hardware or the
emulator), not the concrete `streamdeck` library type -- but `PILHelper`'s
own functions are typed against that concrete class. `PILHelper` only ever
calls `key_image_format()` / `touchscreen_image_format()` on what we pass
it, both of which `DeckDevice` guarantees, so the `cast()` calls below are
purely to satisfy the type checker across that library boundary; they
change no runtime behavior.
"""

from __future__ import annotations

from typing import cast

from PIL import ImageDraw, ImageFont
from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

from .device import DeckDevice

_KEY_LABEL_FONT_SIZE = 16
_STRIP_FONT_SIZE = 22

_ACTIVE_BG = "#1f4f1f"  # dark green -- the currently active session
_INACTIVE_BG = "#1f1f1f"  # dark gray -- other known sessions
_EMPTY_BG = "#000000"  # blank -- no session in this slot
_BELL_COLOR = "#ffcc00"  # amber dot -- bell alert pending
_TEXT_COLOR = "#ffffff"

MAX_SESSION_LABEL_CHARS = 10


def _truncate(name: str, limit: int = MAX_SESSION_LABEL_CHARS) -> str:
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "\u2026"


def render_session_key(
    deck: DeckDevice, name: str, *, active: bool, bell_ringing: bool
) -> bytes:
    """Render one key: session name, green background if active, amber dot if bell ringing."""
    background = _ACTIVE_BG if active else _INACTIVE_BG
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=_KEY_LABEL_FONT_SIZE)

    label = _truncate(name)
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = (
        (image.width - text_w) / 2 - bbox[0],
        (image.height - text_h) / 2 - bbox[1],
    )
    draw.text(position, label, fill=_TEXT_COLOR, font=font)

    if bell_ringing:
        radius = 8
        cx, cy = image.width - radius - 6, radius + 6
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius), fill=_BELL_COLOR
        )

    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


def render_empty_key(deck: DeckDevice) -> bytes:
    """Render a blank (unused) key slot -- no session mapped to it."""
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_EMPTY_BG)
    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


def _paint_full_touchscreen(deck: DeckDevice, image_bytes: bytes) -> None:
    """Paint the entire touch strip.

    The library requires explicit region dimensions for a non-empty image --
    width/height default to 0, which raises `IndexError: Invalid draw width
    0.` This was discovered and fixed in deck_probe/events.py; the same
    requirement applies here.
    """
    width, height = deck.touchscreen_image_format()["size"]
    deck.set_touchscreen_image(image_bytes, 0, 0, width, height)


def render_status_strip(deck: DeckDevice, message: str) -> bytes:
    """Render the touch strip as a single centered status line."""
    image = PILHelper.create_touchscreen_image(
        cast(StreamDeck, deck), background="black"
    )
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=_STRIP_FONT_SIZE)

    bbox = draw.textbbox((0, 0), message, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = (
        (image.width - text_w) / 2 - bbox[0],
        (image.height - text_h) / 2 - bbox[1],
    )
    draw.text(position, message, fill="#ffffff", font=font)

    return PILHelper.to_native_touchscreen_format(cast(StreamDeck, deck), image)


def paint_status_strip(deck: DeckDevice, message: str) -> None:
    """Render and paint the status strip in one call."""
    _paint_full_touchscreen(deck, render_status_strip(deck, message))


def paint_blank_keys(deck: DeckDevice) -> None:
    """Blank every key -- used before a status-only strip message is shown."""
    for index in range(deck.key_count()):
        deck.set_key_image(index, render_empty_key(deck))


def paint_sessions(
    deck: DeckDevice,
    session_names_and_bells: list[tuple[str, bool]],
    active_session: str | None,
) -> None:
    """Paint keys 0..key_count()-1 from a list of (name, bell_ringing) pairs.

    Slots beyond the given list are left blank.
    """
    key_count = deck.key_count()
    for index in range(key_count):
        if index < len(session_names_and_bells):
            name, bell_ringing = session_names_and_bells[index]
            deck.set_key_image(
                index,
                render_session_key(
                    deck,
                    name,
                    active=(name == active_session),
                    bell_ringing=bell_ringing,
                ),
            )
        else:
            deck.set_key_image(index, render_empty_key(deck))
