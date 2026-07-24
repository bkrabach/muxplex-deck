"""Pillow-based rendering helpers for the Stream Deck+ probe.

Every function here is a pure image generator: given a deck (used only for
its declared image formats/dimensions) it returns native image bytes ready
to hand to `StreamDeck.set_key_image` / `StreamDeck.set_touchscreen_image`.
No device I/O happens in this module -- that keeps rendering testable in
isolation and keeps the state machine in main.py free of drawing code.
"""

from __future__ import annotations

from PIL import ImageDraw, ImageFont
from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.ImageHelpers import PILHelper

# Distinct, clearly-differentiable background colors, one per key index.
KEY_COLORS: tuple[str, ...] = (
    "#1f77b4",  # 0 blue
    "#ff7f0e",  # 1 orange
    "#2ca02c",  # 2 green
    "#d62728",  # 3 red
    "#9467bd",  # 4 purple
    "#8c564b",  # 5 brown
    "#e377c2",  # 6 pink
    "#7f7f7f",  # 7 gray
)

# The touch strip is divided into one zone per dial: dial 0 shows live
# brightness (proves set_brightness), the rest show their tap/turn counters.
# Zone count is derived from the deck's dial_count() -- capability-driven,
# not hardcoded to the Plus's 4 dials.
BRIGHTNESS_ZONE_INDEX = 0

_LABEL_FONT_SIZE = 16
_VALUE_FONT_SIZE = 28
# Key labels scale with the key's pixel size: 120px keys (Plus) get the
# original 48px font; 72px keys (Original/MK2) get ~28px. Never assume
# a fixed key resolution.
_KEY_LABEL_FONT_RATIO = 0.4


def zone_labels(dial_count: int) -> tuple[str, ...]:
    """Touchscreen zone labels for a deck with `dial_count` dials."""
    if dial_count <= 0:
        return ()
    return (
        "D0 BRIGHTNESS",
        *(f"D{i} COUNTER" for i in range(1, dial_count)),
    )


def render_key_image(
    deck: StreamDeck, index: int, *, highlighted: bool = False
) -> bytes:
    """Render a key image labeled with its index, in a distinct color.

    When `highlighted` is True, the key is rendered in inverted colors
    (bright text on black) to give clear visual feedback while the physical
    button is held down.
    """
    if highlighted:
        background, text_color = "#000000", "#ffffff"
    else:
        background, text_color = KEY_COLORS[index % len(KEY_COLORS)], "#000000"

    image = PILHelper.create_key_image(deck, background=background)
    draw = ImageDraw.Draw(image)
    # Scale the label to the key's actual pixel size (72px on the 15-key
    # Original/MK2, 96px on the Neo, 120px on the Plus).
    font = ImageFont.load_default(
        size=max(16, int(image.height * _KEY_LABEL_FONT_RATIO))
    )

    label = str(index)
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    position = (
        (image.width - text_w) / 2 - bbox[0],
        (image.height - text_h) / 2 - bbox[1],
    )
    draw.text(position, label, fill=text_color, font=font)

    return PILHelper.to_native_key_format(deck, image)


def _draw_zone(
    draw: ImageDraw.ImageDraw,
    x_offset: int,
    width: int,
    height: int,
    label: str,
    value: str,
) -> None:
    """Draw a zone's label + value, centered within [x_offset, x_offset + width)."""
    label_font = ImageFont.load_default(size=_LABEL_FONT_SIZE)
    value_font = ImageFont.load_default(size=_VALUE_FONT_SIZE)

    label_bbox = draw.textbbox((0, 0), label, font=label_font)
    label_x = x_offset + (width - (label_bbox[2] - label_bbox[0])) / 2
    draw.text((label_x, 8), label, fill="#aaaaaa", font=label_font)

    value_bbox = draw.textbbox((0, 0), value, font=value_font)
    value_x = x_offset + (width - (value_bbox[2] - value_bbox[0])) / 2
    draw.text(
        (value_x, height - _VALUE_FONT_SIZE - 12),
        value,
        fill="#ffffff",
        font=value_font,
    )


def render_touchscreen_full(
    deck: StreamDeck,
    *,
    brightness_percent: int,
    counters: list[int],
    marker_x: int | None = None,
) -> bytes:
    """Render the full touch strip: one labeled zone per dial plus an optional tap marker.

    Only meaningful on touchscreen decks; callers gate on `is_touch()`.
    Zone count follows the deck's dial count (Plus: 4).
    """
    image = PILHelper.create_touchscreen_image(deck, background="black")
    draw = ImageDraw.Draw(image)
    labels = zone_labels(deck.dial_count())
    zone_width = image.width // max(len(labels), 1)

    values = [f"{brightness_percent}%", *[str(c) for c in counters]]
    for zone_index, (label, value) in enumerate(zip(labels, values)):
        x_offset = zone_index * zone_width
        if zone_index > 0:
            draw.line(
                [(x_offset, 0), (x_offset, image.height)], fill="#444444", width=1
            )
        _draw_zone(draw, x_offset, zone_width, image.height, label, value)

    if marker_x is not None:
        draw.line([(marker_x, 0), (marker_x, image.height)], fill="#ffff00", width=3)

    return PILHelper.to_native_touchscreen_format(deck, image)


def render_touchscreen_zone(
    deck: StreamDeck, zone_index: int, label: str, value: str
) -> tuple[bytes, int, int, int, int]:
    """Render a single zone as a standalone image for a partial touchscreen update.

    Returns (native_image_bytes, x_pos, y_pos, width, height) ready to pass
    directly to `StreamDeck.set_touchscreen_image` for the region only --
    proving the device's partial-update support rather than repainting the
    whole 800x100 strip on every dial tick.
    """
    full_width, full_height = deck.touchscreen_image_format()["size"]
    zone_width = full_width // max(deck.dial_count(), 1)
    x_pos = zone_index * zone_width

    zone_image = PILHelper.create_touchscreen_image(deck, background="black")
    zone_image = zone_image.crop((0, 0, zone_width, full_height))
    draw = ImageDraw.Draw(zone_image)
    if zone_index > 0:
        draw.line([(0, 0), (0, full_height)], fill="#444444", width=1)
    _draw_zone(draw, 0, zone_width, full_height, label, value)

    native = PILHelper.to_native_touchscreen_format(deck, zone_image)
    return native, x_pos, 0, zone_width, full_height
