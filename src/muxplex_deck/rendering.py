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

Key face design (v3): every key face -- session tile, control key, picker
option -- is built from the same NAME / BODY / STATE zone model, geometry
computed as f(S) from the face's real pixel edge, never a per-model table.
This is the muxplex-deck key face design system; see
docs/KEY_DESIGN_SYSTEM.md for the full rationale (the collision this fixes,
the type scale, the two orthogonal state channels, and the per-key-type
worked specification this module implements). The short version:

- **Zone model** (`_zone_geometry`): NAME (what this key controls) / BODY
  (the discriminator -- what tells this key from its neighbours) / STATE
  (live ambient context), reserved whether or not they hold ink, inset by
  a margin that always clears the border (`_draw_border`) -- this is what
  fixes the pre-v3 defect where `_fit_label`'s permitted text width let
  session names and control labels draw into the border's own pixels.
- **Type scale** (`_primary_size` / `_secondary_size` / `_TEXTURE_SIZE`):
  three sizes, one weight (`ImageFont.load_default()` has no bold), two
  ink values (`_INK_PRIMARY` white, `_INK_SECONDARY` dim blue-gray) plus a
  third ink (`_INK_TEXTURE`) for the session preview, which is deliberately
  never meant to be read as text.
- **State channels** (`render_session_key`): *active* (this is the live
  session, or the current picker option) is a cyan ring at the face edge,
  costing zero content pixels; *needs attention* is the NAME band's fill
  turning amber with its ink inverted to black, costing zero extra
  pixels since the band already exists. Both can be present at once
  without touching each other -- no state in this system is signalled by
  hue alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from muxplex_client import Session
from PIL import Image, ImageDraw, ImageFont
from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

from .device import DeckDevice

_STRIP_FONT_SIZE = 22

# --- Background fills (what KIND of key this is -- a category, never a
# state; see docs/KEY_DESIGN_SYSTEM.md §4) -------------------------------
_BG_SESSION = "#0a0a0a"  # near-black -- a session lives here
_BG_CONTROL = "#101036"  # dark indigo -- a control or a chooser
_BG_EMPTY = "#000000"  # black -- nothing here

# --- Type-scale ink values (foreground = state/content; see §2/§4) ------
_INK_PRIMARY = "#FFFFFF"  # the one string you actually read
_INK_SECONDARY = "#8888AA"  # known closed vocabulary, recognised not read
_INK_TEXTURE = "#7A7A7A"  # never read; shape recognition only (preview)
_INK_ATTENTION = "#000000"  # inverted ink when the NAME band turns amber

# Brand colors lifted verbatim from muxplex's frontend/style.css so the
# deck's status language matches the PWA's exactly.
_ACTIVE_RING_COLOR = "#00D9F5"  # muxplex brand cyan -- active session/option
_ATTENTION_BAND_COLOR = "#F1A640"  # muxplex brand amber -- needs attention
_ATTENTION_BAND_RGBA = (0xF1, 0xA6, 0x40, 255)  # opaque -- see NAME band fill
_NAME_BANNER_FILL = (0, 0, 0, 195)  # translucent black -- non-attention NAME band

# --- Type scale (docs/KEY_DESIGN_SYSTEM.md §2) --------------------------
# Three sizes, one weight (ImageFont.load_default() is Aileron Regular --
# there is no bold), two ink values plus TEXTURE. PRIMARY/SECONDARY are
# formulas on face edge S; TEXTURE is a fixed 11 REGARDLESS of S -- its
# value is column count in the mini-terminal preview, not apparent size,
# so scaling it up would shrink the hardware-verified 21-column Deck+
# preview (see `_preview_geometry`'s docstring).
_TEXTURE_SIZE = 11


def _primary_size(size: int) -> int:
    """PRIMARY type size for a face of edge `size` -- round(2*S/9)."""
    return round(2 * size / 9)


def _secondary_size(size: int) -> int:
    """SECONDARY type size for a face of edge `size` -- round(11*S/72)."""
    return round(11 * size / 72)


# --- Zone geometry (docs/KEY_DESIGN_SYSTEM.md §1 + §3) ------------------


@dataclass(frozen=True)
class _ZoneGeometry:
    """One face's NAME/BODY/STATE band geometry, computed as f(S).

    Every value here is a formula on the face edge `size` -- never a
    per-model constant (AGENTS.md's capability-driven rule, applied to key
    face geometry). `content_*` describes the single box, inset by
    `margin` on all four sides, that every band's text AND every band's
    background fill live inside; `border` is drawn separately at the face
    edge (inset 0), so the two never collide -- see the module docstring
    and §3's collision writeup.
    """

    size: int
    border: int
    margin: int
    name_top: int
    name_height: int
    body_top: int
    body_height: int
    state_top: int
    state_height: int
    content_left: int
    content_top: int
    content_width: int
    content_height: int


def _zone_geometry(size: int) -> _ZoneGeometry:
    """Compute one face's zone geometry -- see docs/KEY_DESIGN_SYSTEM.md §1.

    `border`/`margin` resolve the state-vs-content collision (§3): the
    pre-v3 code drew the border at inset 0 width 4 while `_fit_label`
    permitted text out to `width - 4`, letting text draw into the
    border's own pixels (worst when both active+attention states applied
    at once, as two concentric rings). Setting `margin = border + gap`
    (here `round(S/18)` against `max(2, round(S/36))`) guarantees content
    fit to `content_width` clears the border for any border width up to
    `margin - 1` -- the design survives a thicker border (e.g. B=3 at
    S=72) without any other change.
    """
    border = max(2, round(size / 36))
    margin = round(size / 18)
    name_height = round(0.28 * size)
    state_height = round(0.19 * size)
    body_height = size - 2 * margin - name_height - state_height
    name_top = margin
    body_top = name_top + name_height
    state_top = body_top + body_height
    return _ZoneGeometry(
        size=size,
        border=border,
        margin=margin,
        name_top=name_top,
        name_height=name_height,
        body_top=body_top,
        body_height=body_height,
        state_top=state_top,
        state_height=state_height,
        content_left=margin,
        content_top=margin,
        content_width=size - 2 * margin,
        content_height=size - 2 * margin,
    )


# Fixed reference glyph for vertical centring -- see `_band_text_position`.
_VCENTER_REFERENCE = "Hxg"


def _band_text_position(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    *,
    content_left: int,
    content_width: int,
    band_top: int,
    band_height: int,
) -> tuple[float, float]:
    """(x, y) that horizontally centres `text` and vertically centres the
    band on a FIXED reference glyph's ink bbox, never `text`'s own bbox.

    Centring each string's own bbox (the pre-v3 behavior) makes a label
    with a descender sit ~2px higher than one without, so adjacent keys
    look vertically inconsistent -- see docs/KEY_DESIGN_SYSTEM.md §1's
    "prevents per-key jitter" note. Horizontal centring still uses the
    actual text's own width -- only the vertical anchor is fixed.
    """
    ref_bbox = draw.textbbox((0, 0), _VCENTER_REFERENCE, font=font)
    ref_height = ref_bbox[3] - ref_bbox[1]
    y = band_top + (band_height - ref_height) / 2 - ref_bbox[1]

    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    x = content_left + (content_width - text_width) / 2 - text_bbox[0]
    return x, y


def _draw_band_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    *,
    content_left: int,
    content_width: int,
    band_top: int,
    band_height: int,
) -> None:
    """Draw `text` centred in one zone-model band. No-op for empty text --
    reserved-but-empty bands (e.g. a picker option's NAME) draw nothing."""
    if not text:
        return
    x, y = _band_text_position(
        draw,
        text,
        font,
        content_left=content_left,
        content_width=content_width,
        band_top=band_top,
        band_height=band_height,
    )
    draw.text((x, y), text, fill=fill, font=font)


def _fit_label(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: float,
) -> str:
    """Shorten `text` (with an ellipsis) until it fits `max_width` pixels.

    The sole truncation gate (v3): a fixed character-count pre-cap
    (`MAX_SESSION_LABEL_CHARS = 10` for session names, a matching cap for
    picker labels) used to run BEFORE this and was tuned for 120px keys --
    on a 72px face it was either too loose (never the binding limit,
    dead weight) or, worse, wrong in the other direction for longer
    labels. Measured pixel fit is honest at every face size; see
    docs/KEY_DESIGN_SYSTEM.md §5.
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

    v3 draws at most ONE ring per face (active session, or the current
    picker option) -- the pre-v3 dual-concentric-ring encoding for
    "active AND needs-attention simultaneously" is gone; attention is now
    its own orthogonal channel (the NAME band fill in `render_session_key`),
    so it never needs a second ring. See docs/KEY_DESIGN_SYSTEM.md §3.
    """
    for w in range(width):
        offset = inset + w
        draw.rectangle(
            [(offset, offset), (image.width - 1 - offset, image.height - 1 - offset)],
            outline=color,
        )


# --- Mini terminal preview (session tile BODY+STATE field) -------------

_PREVIEW_LINE_HEIGHT = 13
# Approximate advance width of one monospace character at `_TEXTURE_SIZE`,
# used to derive how many columns fit the key's real width (see
# `_preview_geometry`). 5.5 is anchored so a 120px key yields exactly the
# 21 columns the hardware-verified Stream Deck+ path always used.
_PREVIEW_CHAR_WIDTH = 5.5
_PREVIEW_LEFT_MARGIN = 3

# Matches ANSI CSI sequences (colors, cursor moves, etc.) -- v1 strips all
# color/formatting rather than rendering it; see module docstring. Covers
# what `tmux capture-pane -e` actually emits (SGR color codes); a fancier
# fidelity pass (the PWA's own SGR parser, app.js) is a documented follow-up,
# not attempted here.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _preview_geometry(width: int, content_height: int) -> tuple[int, int]:
    """(lines, columns) of preview crop that fit a key of the given real size.

    `content_height` is the zone-model content-box height (`size - 2 *
    margin`, from `_zone_geometry`) -- this replaces the pre-v3
    banner-relative `height - _BANNER_HEIGHT` and is the "computing preview
    line count against the full content height" change
    docs/KEY_DESIGN_SYSTEM.md §1 calls out; it reproduces the identical
    hardware-verified 4 lines at S=72 / 8 lines at S=120 the old formula
    did, with zero preview lines lost to the border-collision margin fix.

    Column count is intentionally left on its ORIGINAL basis (`width` and
    `_PREVIEW_LEFT_MARGIN`, not the zone-model content width): §5 warns
    against reducing the hardware-verified 21-column Deck+ count, and that
    warning is specifically about not scaling `_TEXTURE_SIZE` with `S` --
    it does not ask for the column formula's margin to change too.
    Switching columns to the content-box width would drop Deck+ from 21 to
    19 columns, an unrequested and un-hardware-verified regression, so the
    column formula is left untouched.
    """
    lines = max(1, content_height // _PREVIEW_LINE_HEIGHT)
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

    Honest fidelity note: at ~5px/character this crop is TEXTURE, not
    text -- "recognize your session by its shape", not "read the text".
    See docs/KEY_DESIGN_SYSTEM.md §5's "honest ceiling".
    """
    plain = _strip_ansi(snapshot)
    lines = plain.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    tail = lines[-max_lines:] if max_lines else []
    return [line[:max_columns] for line in tail]


def render_session_key(deck: DeckDevice, session: Session, *, active: bool) -> bytes:
    """Render one key: mini terminal preview, NAME band, active/attention state.

    Layering (bottom to top): near-black background -> preview text,
    field-underlaid across the full content box, bottom-anchored ->
    NAME band (translucent black normally; opaque amber with inverted
    ink if the session needs attention) -> cyan ring at the face edge if
    this is the active session. NAME is the only band this face's PRIMARY
    string lives in -- the key *is* that session, the name is what you
    hunt for, and the preview beneath is TEXTURE (see
    docs/KEY_DESIGN_SYSTEM.md §6.1).
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_BG_SESSION)
    draw = ImageDraw.Draw(image)
    size = image.width
    geo = _zone_geometry(size)
    preview_font = ImageFont.load_default(size=_TEXTURE_SIZE)

    max_lines, max_columns = _preview_geometry(image.width, geo.content_height)
    lines = _preview_lines(session.snapshot, max_lines, max_columns)
    content_bottom = geo.content_top + geo.content_height
    base_y = content_bottom - len(lines) * _PREVIEW_LINE_HEIGHT
    for row, line in enumerate(lines):
        if not line:
            continue
        draw.text(
            (geo.content_left, base_y + row * _PREVIEW_LINE_HEIGHT),
            line,
            fill=_INK_TEXTURE,
            font=preview_font,
        )

    # NAME band: needs an RGBA overlay composited onto the (opaque RGB)
    # preview, then flattened back to RGB for the native format -- same
    # reason the pre-v3 translucent banner needed one.
    name_font = ImageFont.load_default(size=_primary_size(size))
    label = _fit_label(draw, session.name, name_font, geo.content_width)

    needs_attention = session.bell.needs_attention
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    band_box = [
        (geo.content_left, geo.name_top),
        (geo.content_left + geo.content_width, geo.name_top + geo.name_height),
    ]
    if needs_attention:
        # Split state channel (docs/KEY_DESIGN_SYSTEM.md §3): fill turns
        # amber, ink inverts to black (~10.4:1 contrast) -- signalled by
        # BOTH fill and ink polarity, never hue alone.
        ImageDraw.Draw(overlay).rectangle(band_box, fill=_ATTENTION_BAND_RGBA)
        name_ink = _INK_ATTENTION
    else:
        ImageDraw.Draw(overlay).rectangle(band_box, fill=_NAME_BANNER_FILL)
        name_ink = _INK_PRIMARY
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)
    _draw_band_text(
        draw,
        label,
        name_font,
        name_ink,
        content_left=geo.content_left,
        content_width=geo.content_width,
        band_top=geo.name_top,
        band_height=geo.name_height,
    )

    if active:
        # The other split state channel: a ring at the face edge, costing
        # zero content pixels -- lives entirely in the margin the NAME
        # band is inset by, so it never touches the band it's next to.
        _draw_border(draw, image, _ACTIVE_RING_COLOR, geo.border)

    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


def render_empty_key(deck: DeckDevice) -> bytes:
    """Render a blank (unused) key slot -- no session mapped to it."""
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_BG_EMPTY)
    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


def render_picker_key(deck: DeckDevice, label: str, *, current: bool) -> bytes:
    """Render one picker-mode key: a centered label on a distinct background.

    Shared by both the view picker (dial 0 press) and the page picker
    (dial 1 press) -- `label` is a view name or a page number as a string.
    `current` draws the same cyan ring used for the active session,
    marking whichever option matches today's actual active view/page.

    Deliberate exception to "NAME always occupied" (§6.3): NAME and STATE
    are reserved but always empty here -- in picker mode every key is the
    same category, so a repeated category label would be pure noise.
    `label` is the BODY (the discriminator), drawn at PRIMARY size in the
    BODY band.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_BG_CONTROL)
    draw = ImageDraw.Draw(image)
    size = image.width
    geo = _zone_geometry(size)
    font = ImageFont.load_default(size=_primary_size(size))
    text = _fit_label(draw, label, font, geo.content_width)
    _draw_band_text(
        draw,
        text,
        font,
        _INK_PRIMARY,
        content_left=geo.content_left,
        content_width=geo.content_width,
        band_top=geo.body_top,
        band_height=geo.body_height,
    )
    if current:
        _draw_border(draw, image, _ACTIVE_RING_COLOR, geo.border)
    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


# --- Control keys (dial-less decks, and the reduced-layout picker chrome) ---
#
# On decks with no dials/touch strip (Original/MK2/XL/Mini), three keys are
# reserved for the roles the dials and strip played -- see `layout.py`; the
# same rendering also paints the reduced-layout picker's BACK/PREV/NEXT
# chrome. They reuse the picker's indigo background so "control chrome" is
# visually distinct from session tiles, and their labels are ASCII-only:
# the default PIL font has no glyphs for arrows and renders .notdef boxes
# instead (proven on real hardware with U+2192).


def render_control_key(
    deck: DeckDevice, *, name: str, body: str, state: str = ""
) -> bytes:
    """Render one control key from its (NAME, BODY, STATE) zone content.

    Every control action's display resolves to this same three-band
    layout -- see `main._control_key_display` and
    docs/KEY_DESIGN_SYSTEM.md §6.2 for the full per-action table. NAME and
    STATE are SECONDARY (dim, recognised-not-read); BODY is PRIMARY (the
    one string you actually read) -- this is the "discriminator swap" from
    pre-v3: the noun (`VIEW`/`PAGE`) is now the large BODY text and the
    direction (`< PREV`/`NEXT >`) is the small NAME caption, since two
    adjacent `< PREV` keys distinguished only by a dim caption were the
    exact complaint this design system exists to fix.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_BG_CONTROL)
    draw = ImageDraw.Draw(image)
    size = image.width
    geo = _zone_geometry(size)

    if name:
        font = ImageFont.load_default(size=_secondary_size(size))
        text = _fit_label(draw, name, font, geo.content_width)
        _draw_band_text(
            draw,
            text,
            font,
            _INK_SECONDARY,
            content_left=geo.content_left,
            content_width=geo.content_width,
            band_top=geo.name_top,
            band_height=geo.name_height,
        )

    if body:
        font = ImageFont.load_default(size=_primary_size(size))
        text = _fit_label(draw, body, font, geo.content_width)
        _draw_band_text(
            draw,
            text,
            font,
            _INK_PRIMARY,
            content_left=geo.content_left,
            content_width=geo.content_width,
            band_top=geo.body_top,
            band_height=geo.body_height,
        )

    if state:
        font = ImageFont.load_default(size=_secondary_size(size))
        text = _fit_label(draw, state, font, geo.content_width)
        _draw_band_text(
            draw,
            text,
            font,
            _INK_SECONDARY,
            content_left=geo.content_left,
            content_width=geo.content_width,
            band_top=geo.state_top,
            band_height=geo.state_height,
        )

    return PILHelper.to_native_key_format(cast(StreamDeck, deck), image)


_STATUS_KEY_LINE_HEIGHT = 13


def render_status_key(deck: DeckDevice, message: str) -> bytes:
    """Render a status message word-wrapped onto a single key.

    Decks without a touch strip have nowhere else to show AUTH FAILED /
    UNREACHABLE states, so the message is wrapped onto one key (the VIEW
    key's position) at SECONDARY size -- honest signal over composition;
    the log carries the full detail. Deliberately breaks the zone model
    (docs/KEY_DESIGN_SYSTEM.md §6.5): an error message is free-form text of
    unknown length and there is nothing else on the face to align to, so
    it fills the whole content box rather than being confined to one band.
    """
    image = PILHelper.create_key_image(cast(StreamDeck, deck), background=_BG_EMPTY)
    draw = ImageDraw.Draw(image)
    size = image.width
    geo = _zone_geometry(size)
    font = ImageFont.load_default(size=_secondary_size(size))
    max_width = geo.content_width
    max_lines = max(1, geo.content_height // _STATUS_KEY_LINE_HEIGHT)

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
            (geo.content_left, geo.content_top + row * _STATUS_KEY_LINE_HEIGHT),
            _fit_label(draw, line, font, max_width),
            fill=_INK_SECONDARY,
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
    """Render the touch strip as a single centered status line.

    Outside the key face zone model (docs/KEY_DESIGN_SYSTEM.md is
    explicitly scoped to LCD keys) -- unchanged from pre-v3.
    """
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
