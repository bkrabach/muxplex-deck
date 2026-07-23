"""Pillow-based rendering for the muxplex sidecar.

Adapted from `deck_probe/rendering.py`'s proven patterns (native image
conversion via `PILHelper`, explicit touch-strip region size) but drawing
session mini-terminal previews and server status instead of probe
diagnostics. No device I/O happens here -- these are pure image generators,
kept separate from `main.py`'s state machine so rendering stays testable in
isolation.

Functions here accept the `DeckDevice` protocol (real hardware or the
emulator), not the concrete `streamdeck` library type -- but `PILHelper`'s
own functions are typed against that concrete class. `PILHelper` only ever
calls `key_image_format()` / `touchscreen_image_format()` on what we pass
it, both of which `DeckDevice` guarantees, so the `cast()` calls below are
purely to satisfy the type checker across that library boundary; they
change no runtime behavior.

Key preview design (v1): each occupied key shows a cropped mini terminal --
the bottom-left corner of the session's live pane snapshot -- with the
session name, active-border highlight, and bell dot layered on top so
identity/status stay legible regardless of what's scrolling underneath.
ANSI color escapes are stripped (plain text only); see `_strip_ansi` and
the module-level fidelity note below `_preview_lines`.
"""

from __future__ import annotations

import re
from typing import cast

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

from .client import Session
from .device import DeckDevice

_KEY_LABEL_FONT_SIZE = 16
_STRIP_FONT_SIZE = 22

_EMPTY_BG = "#000000"  # blank -- no session in this slot
_BELL_COLOR = "#ffcc00"  # amber dot -- bell alert pending
_TEXT_COLOR = "#ffffff"

MAX_SESSION_LABEL_CHARS = 10

# --- Mini terminal preview -------------------------------------------------

_PREVIEW_BG = "#0a0a0a"  # near-black -- the preview's own background
_PREVIEW_TEXT_COLOR = "#a8a8a8"  # light gray -- low-contrast so name/badges pop
_PREVIEW_FONT_SIZE = 8
_PREVIEW_LINE_HEIGHT = 10
_PREVIEW_LINES = 9  # bottom N lines of the snapshot
_PREVIEW_COLUMNS = 26  # first N columns of each of those lines (bottom-LEFT crop)
_PREVIEW_LEFT_MARGIN = 3

_BANNER_HEIGHT = 20  # translucent strip behind the session name, top of key
_BANNER_FILL = (0, 0, 0, 170)  # RGBA -- ~two-thirds-opaque black

_ACTIVE_BORDER_COLOR = "#33dd33"  # bright green -- replaces the old full-bg fill
_ACTIVE_BORDER_WIDTH = 4

# Matches ANSI CSI sequences (colors, cursor moves, etc.) -- v1 strips all
# color/formatting rather than rendering it; see module docstring. Covers
# what `tmux capture-pane -e` actually emits (SGR color codes); a fancier
# fidelity pass (the PWA's own SGR parser, app.js) is a documented follow-up,
# not attempted here.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _truncate(name: str, limit: int = MAX_SESSION_LABEL_CHARS) -> str:
    if len(name) <= limit:
        return name
    return name[: limit - 1] + "\u2026"


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _preview_lines(snapshot: str) -> list[str]:
    """Bottom-left crop of a session's snapshot, ready to draw line-by-line.

    Strips ANSI escapes, trims trailing blank lines (the snapshot is a
    fixed-height `tmux capture-pane` and commonly ends with several once a
    session's actual output is shorter than the capture window), then keeps
    the last `_PREVIEW_LINES` lines and the first `_PREVIEW_COLUMNS`
    columns of each -- bottom-left, chosen over the PWA's bottom-anchored
    *full-width* preview because a square 120px key has no width to spare.

    Honest fidelity note: at ~5px/character this crop is "recognize your
    session by its shape and color" (well, grayscale in v1), not "read the
    text" -- the same tradeoff the PWA's own thumbnails make, just smaller.
    """
    plain = _strip_ansi(snapshot)
    lines = plain.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    tail = lines[-_PREVIEW_LINES:] if _PREVIEW_LINES else []
    return [line[:_PREVIEW_COLUMNS] for line in tail]


def render_session_key(deck: DeckDevice, session: Session, *, active: bool) -> bytes:
    """Render one key: mini terminal preview, name banner, active border, bell dot.

    Layering (bottom to top): near-black background -> preview text ->
    translucent name banner + name -> bell dot -> active border. The last
    three stay legible no matter what's scrolling in the preview beneath
    them.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_PREVIEW_BG)
    draw = ImageDraw.Draw(image)
    preview_font = ImageFont.load_default(size=_PREVIEW_FONT_SIZE)

    lines = _preview_lines(session.snapshot)
    base_y = image.height - len(lines) * _PREVIEW_LINE_HEIGHT - 2
    for row, line in enumerate(lines):
        if not line:
            continue
        draw.text(
            (_PREVIEW_LEFT_MARGIN, base_y + row * _PREVIEW_LINE_HEIGHT),
            line,
            fill=_PREVIEW_TEXT_COLOR,
            font=preview_font,
        )

    # Translucent name banner -- needs an RGBA overlay composited onto the
    # (opaque RGB) preview, then flattened back to RGB for the native format.
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle(
        [(0, 0), (image.width, _BANNER_HEIGHT)], fill=_BANNER_FILL
    )
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)

    name_font = ImageFont.load_default(size=_KEY_LABEL_FONT_SIZE)
    label = _truncate(session.name)
    bbox = draw.textbbox((0, 0), label, font=name_font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    name_position = (
        (image.width - text_w) / 2 - bbox[0],
        (_BANNER_HEIGHT - text_h) / 2 - bbox[1],
    )
    draw.text(name_position, label, fill=_TEXT_COLOR, font=name_font)

    if session.bell.needs_attention:
        radius = 8
        cx, cy = image.width - radius - 6, radius + 6
        draw.ellipse(
            (cx - radius, cy - radius, cx + radius, cy + radius), fill=_BELL_COLOR
        )

    if active:
        for w in range(_ACTIVE_BORDER_WIDTH):
            draw.rectangle(
                [(w, w), (image.width - 1 - w, image.height - 1 - w)],
                outline=_ACTIVE_BORDER_COLOR,
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
