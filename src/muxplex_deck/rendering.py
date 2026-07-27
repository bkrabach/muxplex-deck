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
session name, active/attention status border(s), layered on top so
identity/status stay legible regardless of what's scrolling underneath.
ANSI color escapes are stripped (plain text only); see `_strip_ansi` and
the module-level fidelity note below `_preview_lines`.

Status borders (v2, real-hardware feedback): active-session and
needs-attention are shown as colored rectangular borders rather than a
background fill or a small dot, using the exact brand colors from
muxplex's own frontend (`frontend/style.css`) so the deck's language
matches the PWA's: cyan `#00D9F5` for the active session, amber `#F1A640`
for needs-attention. When both apply to the same key, two concentric rings
are drawn (amber outer, cyan inner) so neither state is lost -- see
`render_session_key`.
"""

from __future__ import annotations

import re
from typing import cast

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

from muxplex_client import Session
from .device import DeckDevice

_KEY_LABEL_FONT_SIZE = 16
_STRIP_FONT_SIZE = 22

_EMPTY_BG = "#000000"  # blank -- no session in this slot
_TEXT_COLOR = "#ffffff"

MAX_SESSION_LABEL_CHARS = 10

# --- Mini terminal preview -------------------------------------------------

_PREVIEW_BG = "#0a0a0a"  # near-black -- the preview's own background
_PREVIEW_TEXT_COLOR = "#a8a8a8"  # light gray -- low-contrast so name/badges pop
# v3 (real-hardware feedback: v1's size-8 was illegible, v2's size-16 too
# zoomed-in) -- split the difference at ~12, with line/column counts scaled to
# match so the crop still fits under the banner within the 120x120 key. Still
# "recognize your session by its shape", not "read the text" -- see the fidelity
# note in `_preview_lines` below.
_PREVIEW_FONT_SIZE = 11
_PREVIEW_LINE_HEIGHT = 13
# Approximate advance width of one monospace character at _PREVIEW_FONT_SIZE,
# used to derive how many columns fit the key's real width (see
# `_preview_geometry`). 5.5 is anchored so a 120px key yields exactly the 21
# columns the hardware-verified Stream Deck+ path always used.
_PREVIEW_CHAR_WIDTH = 5.5
_PREVIEW_LEFT_MARGIN = 3

_BANNER_HEIGHT = 20  # translucent strip behind the session name, top of key
_BANNER_FILL = (0, 0, 0, 170)  # RGBA -- ~two-thirds-opaque black

# Brand colors lifted verbatim from muxplex's frontend/style.css so the
# deck's status language matches the PWA's exactly.
_ACTIVE_BORDER_COLOR = "#00D9F5"  # muxplex brand cyan -- active session
_ATTENTION_BORDER_COLOR = "#F1A640"  # muxplex brand amber -- needs attention
_BORDER_WIDTH = 4  # single-state border thickness
_DUAL_RING_WIDTH = 3  # each ring's thickness when both states apply at once

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


def _preview_geometry(width: int, height: int) -> tuple[int, int]:
    """(lines, columns) of preview crop that fit a key of the given real size.

    Derived from the deck's actual `key_image_format()['size']` at render
    time -- never assumes 120px. The formulas are anchored so the
    hardware-verified 120x120 Stream Deck+ path yields exactly the 8 lines
    x 21 columns the previous fixed constants produced (no Deck+ regression),
    while a 72x72 Original/MK2 key scales down (4 lines x 12 columns)
    instead of overflowing the key.
    """
    lines = max(1, (height - _BANNER_HEIGHT - 2) // _PREVIEW_LINE_HEIGHT + 1)
    columns = max(1, int((width - _PREVIEW_LEFT_MARGIN) / _PREVIEW_CHAR_WIDTH))
    return lines, columns


def _preview_lines(snapshot: str, max_lines: int, max_columns: int) -> list[str]:
    """Bottom-left crop of a session's snapshot, ready to draw line-by-line.

    Strips ANSI escapes, trims trailing blank lines (the snapshot is a
    fixed-height `tmux capture-pane` and commonly ends with several once a
    session's actual output is shorter than the capture window), then keeps
    the last `max_lines` lines and the first `max_columns` columns of each
    -- bottom-left, chosen over the PWA's bottom-anchored *full-width*
    preview because a square key has no width to spare. `max_lines` /
    `max_columns` come from `_preview_geometry` (the key's real pixels).

    Honest fidelity note: at ~5px/character this crop is "recognize your
    session by its shape and color" (well, grayscale in v1), not "read the
    text" -- the same tradeoff the PWA's own thumbnails make, just smaller.
    """
    plain = _strip_ansi(snapshot)
    lines = plain.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    tail = lines[-max_lines:] if max_lines else []
    return [line[:max_columns] for line in tail]


def _fit_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: float,
) -> str:
    """Shorten `text` (with an ellipsis) until it fits `max_width` pixels.

    Complements the fixed character-count `_truncate` caps: those were
    tuned on 120px keys, so on smaller keys (72px Original) a 10-char name
    could still overflow. Measures the real rendered width instead.
    """
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "\u2026", font=font) > max_width:
        text = text[:-1]
    return text + "\u2026"


def _draw_border(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    color: str,
    width: int,
    *,
    inset: int = 0,
) -> None:
    """Draw a `width`-px rectangular outline, `inset` px in from the key's edge.

    Used for both the single-state active/attention border and, when both
    states apply, two concentric rings (an `inset=0` outer ring and an
    `inset=<outer width>` inner ring) -- see `render_session_key`.
    """
    for w in range(width):
        offset = inset + w
        draw.rectangle(
            [(offset, offset), (image.width - 1 - offset, image.height - 1 - offset)],
            outline=color,
        )


def render_session_key(deck: DeckDevice, session: Session, *, active: bool) -> bytes:
    """Render one key: mini terminal preview, name banner, status border(s).

    Layering (bottom to top): near-black background -> preview text ->
    translucent name banner + name -> status border(s) (cyan for active,
    amber for needs-attention, both as concentric rings if both apply). The
    banner and border stay legible no matter what's scrolling in the
    preview beneath them.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_PREVIEW_BG)
    draw = ImageDraw.Draw(image)
    preview_font = ImageFont.load_default(size=_PREVIEW_FONT_SIZE)

    max_lines, max_columns = _preview_geometry(image.width, image.height)
    lines = _preview_lines(session.snapshot, max_lines, max_columns)
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
    label = _fit_label(draw, _truncate(session.name), name_font, image.width - 4)
    bbox = draw.textbbox((0, 0), label, font=name_font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    name_position = (
        (image.width - text_w) / 2 - bbox[0],
        (_BANNER_HEIGHT - text_h) / 2 - bbox[1],
    )
    draw.text(name_position, label, fill=_TEXT_COLOR, font=name_font)

    needs_attention = session.bell.needs_attention
    if active and needs_attention:
        # Both states at once: amber outer ring + cyan inner ring, each
        # legible on its own -- neither status gets silently dropped.
        _draw_border(draw, image, _ATTENTION_BORDER_COLOR, _DUAL_RING_WIDTH)
        _draw_border(
            draw,
            image,
            _ACTIVE_BORDER_COLOR,
            _DUAL_RING_WIDTH,
            inset=_DUAL_RING_WIDTH,
        )
    elif active:
        _draw_border(draw, image, _ACTIVE_BORDER_COLOR, _BORDER_WIDTH)
    elif needs_attention:
        _draw_border(draw, image, _ATTENTION_BORDER_COLOR, _BORDER_WIDTH)

    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


def render_empty_key(deck: DeckDevice) -> bytes:
    """Render a blank (unused) key slot -- no session mapped to it."""
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_EMPTY_BG)
    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


_PICKER_BG = "#101036"  # dark indigo -- visually distinct from the session preview bg
_PICKER_TEXT_COLOR = "#ffffff"
_PICKER_FONT_SIZE = 20
_PICKER_LABEL_CHARS = 12
_PICKER_CURRENT_BORDER_COLOR = _ACTIVE_BORDER_COLOR  # same cyan as the active session
_PICKER_CURRENT_BORDER_WIDTH = _BORDER_WIDTH


def render_picker_key(deck: DeckDevice, label: str, *, current: bool) -> bytes:
    """Render one picker-mode key: a centered label on a distinct background.

    Shared by both the view picker (dial 0 press) and the page picker
    (dial 1 press) -- `label` is a view name or a page number as a string.
    `current` draws the same cyan border used for the active session,
    marking whichever option matches today's actual active view/page.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_PICKER_BG)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=_PICKER_FONT_SIZE)
    text = _fit_label(
        draw, _truncate(label, limit=_PICKER_LABEL_CHARS), font, image.width - 4
    )
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = (
        (image.width - text_w) / 2 - bbox[0],
        (image.height - text_h) / 2 - bbox[1],
    )
    draw.text(position, text, fill=_PICKER_TEXT_COLOR, font=font)
    if current:
        _draw_border(
            draw, image, _PICKER_CURRENT_BORDER_COLOR, _PICKER_CURRENT_BORDER_WIDTH
        )
    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


# --- Reserved control keys (dial-less decks) --------------------------------
#
# On decks with no dials/touch strip (Original/MK2/XL/Mini), three keys are
# reserved for the roles the dials and strip played -- see `layout.py`. They
# reuse the picker's indigo background so "control chrome" is visually
# distinct from session tiles, and their labels are ASCII-only: the default
# PIL font has no glyphs for arrows and renders .notdef boxes instead
# (proven on real hardware with U+2192).

_CONTROL_BG = _PICKER_BG
_CONTROL_TEXT_COLOR = "#ffffff"
_CONTROL_DIM_COLOR = "#8888aa"
_CONTROL_TITLE_FONT_SIZE = 11
_CONTROL_BODY_FONT_SIZE = 15
_CONTROL_FOOTER_FONT_SIZE = 11


def render_control_key(
    deck: DeckDevice, *, title: str, body: str, footer: str = ""
) -> bytes:
    """Render one reserved control key (VIEW / PREV / NEXT).

    Three stacked rows on the control background, each fit to the key's
    *real* width (72px Original and 120px Plus both work):
    - `title`: small dim caption at the top (e.g. "VIEW").
    - `body`: the prominent centered label (view name, "< PREV", "NEXT >").
    - `footer`: small dim line at the bottom (server label, or "pN/M").
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_CONTROL_BG)
    draw = ImageDraw.Draw(image)
    max_width = image.width - 4

    if title:
        font = ImageFont.load_default(size=_CONTROL_TITLE_FONT_SIZE)
        text = _fit_label(draw, title, font, max_width)
        width = draw.textlength(text, font=font)
        draw.text(
            ((image.width - width) / 2, 2), text, fill=_CONTROL_DIM_COLOR, font=font
        )

    body_font = ImageFont.load_default(size=_CONTROL_BODY_FONT_SIZE)
    text = _fit_label(draw, body, body_font, max_width)
    bbox = draw.textbbox((0, 0), text, font=body_font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((image.width - text_w) / 2 - bbox[0], (image.height - text_h) / 2 - bbox[1]),
        text,
        fill=_CONTROL_TEXT_COLOR,
        font=body_font,
    )

    if footer:
        font = ImageFont.load_default(size=_CONTROL_FOOTER_FONT_SIZE)
        text = _fit_label(draw, footer, font, max_width)
        width = draw.textlength(text, font=font)
        draw.text(
            (
                (image.width - width) / 2,
                image.height - _CONTROL_FOOTER_FONT_SIZE - 4,
            ),
            text,
            fill=_CONTROL_DIM_COLOR,
            font=font,
        )

    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


_STATUS_KEY_FONT_SIZE = 11
_STATUS_KEY_LINE_HEIGHT = 13
_STATUS_KEY_MARGIN = 3


def render_status_key(deck: DeckDevice, message: str) -> bytes:
    """Render a status message word-wrapped onto a single key.

    Decks without a touch strip have nowhere else to show AUTH FAILED /
    UNREACHABLE states, so the message is wrapped onto one key (the VIEW
    key's position) at small size -- honest signal over polish; the log
    carries the full detail.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_EMPTY_BG)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=_STATUS_KEY_FONT_SIZE)
    max_width = image.width - 2 * _STATUS_KEY_MARGIN
    max_lines = max(
        1, (image.height - 2 * _STATUS_KEY_MARGIN) // _STATUS_KEY_LINE_HEIGHT
    )

    lines: list[str] = []
    current = ""
    for word in message.split():
        candidate = f"{current} {word}".strip()
        if not current or draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    for row, line in enumerate(lines):
        draw.text(
            (_STATUS_KEY_MARGIN, _STATUS_KEY_MARGIN + row * _STATUS_KEY_LINE_HEIGHT),
            _fit_label(draw, line, font, max_width),
            fill=_TEXT_COLOR,
            font=font,
        )

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
