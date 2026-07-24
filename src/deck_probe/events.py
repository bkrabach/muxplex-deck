"""Device-event handling for the Stream Deck probe (any model).

Owns the small piece of mutable session state each control's callback needs
(dial counters, current brightness, last tap marker) and wires the
`streamdeck` library's key/dial/touchscreen callbacks to logging plus
`rendering.py` calls that reflect the new state back onto the device.

Everything is capability-gated: keys are always exercised (sized from the
deck's own `key_image_format()`), dials only when `dial_count() > 0`, the
touch strip only when `is_touch()`. Branching is on numeric/boolean
capabilities -- never on `deck_type()` strings.

No hotplug/lifecycle logic lives here -- that's main.py's job. This module
only cares about "device is open, here's what happened, here's how to show it."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from StreamDeck.Devices.StreamDeck import (
    DialEventType,
    StreamDeck,
    TouchscreenEventType,
)

from . import rendering

logger = logging.getLogger("deck_probe")

BRIGHTNESS_DIAL_INDEX = 0
DEFAULT_BRIGHTNESS_PERCENT = 75


@dataclass
class ProbeState:
    """Mutable runtime state for one active (connected) probe session."""

    brightness_percent: int = DEFAULT_BRIGHTNESS_PERCENT
    dial_counters: list[int] = field(default_factory=list)
    marker_x: int | None = None


def _paint_full_touchscreen(deck: StreamDeck, image: bytes) -> None:
    """Paint the entire touch strip.

    Only call on decks where `is_touch()` is True. The library requires
    explicit region dimensions for a non-empty image (width/height default
    to 0, which raises `IndexError: Invalid draw width 0.`), so full
    repaints must pass the strip's full size.
    """
    width, height = deck.touchscreen_image_format()["size"]
    deck.set_touchscreen_image(image, 0, 0, width, height)


def paint_initial_state(deck: StreamDeck) -> ProbeState:
    """Paint keys (and the touch strip, when present) for a fresh connection.

    Returns the fresh `ProbeState` that callbacks should mutate for the
    lifetime of this connection. Adapts to the connected model: key count
    and image size come from the device, and the touch strip is painted
    only when the deck has one.
    """
    state = ProbeState(dial_counters=[0] * deck.dial_count())
    with deck:
        deck.set_brightness(state.brightness_percent)
        for index in range(deck.key_count()):
            deck.set_key_image(index, rendering.render_key_image(deck, index))
        if deck.is_touch():
            _paint_full_touchscreen(
                deck,
                rendering.render_touchscreen_full(
                    deck,
                    brightness_percent=state.brightness_percent,
                    counters=state.dial_counters,
                ),
            )
    return state


def _zone_label_and_value(
    state: ProbeState, dial_index: int, labels: tuple[str, ...]
) -> tuple[str, str]:
    if dial_index == BRIGHTNESS_DIAL_INDEX:
        return labels[0], f"{state.brightness_percent}%"
    return labels[dial_index], str(state.dial_counters[dial_index])


def _redraw_zone(deck: StreamDeck, state: ProbeState, dial_index: int) -> None:
    labels = rendering.zone_labels(deck.dial_count())
    label, value = _zone_label_and_value(state, dial_index, labels)
    image, x, y, width, height = rendering.render_touchscreen_zone(
        deck, dial_index, label, value
    )
    deck.set_touchscreen_image(image, x, y, width, height)


def make_key_callback():
    """Build the key-press callback: log + invert colors while held."""

    def on_key(deck: StreamDeck, key: int, pressed: bool) -> None:
        logger.info("key[%d] %s", key, "PRESSED" if pressed else "released")
        deck.set_key_image(
            key, rendering.render_key_image(deck, key, highlighted=pressed)
        )

    return on_key


def make_dial_callback(state: ProbeState):
    """Build the dial callback: dial 0 drives brightness, the rest are counters.

    Zone repaints on the touch strip happen only when the deck has one
    (`is_touch()`); dial-only decks still get full logging + brightness.
    """

    def on_dial(
        deck: StreamDeck, dial: int, event_type: DialEventType, value: object
    ) -> None:
        if event_type == DialEventType.TURN:
            amount = int(value)  # type: ignore[arg-type]
            direction = "clockwise" if amount > 0 else "counter-clockwise"
            logger.info("dial[%d] TURN %+d (%s)", dial, amount, direction)
            with deck:
                if dial == BRIGHTNESS_DIAL_INDEX:
                    state.brightness_percent = max(
                        0, min(100, state.brightness_percent + amount)
                    )
                    deck.set_brightness(state.brightness_percent)
                else:
                    state.dial_counters[dial] += amount
                if deck.is_touch():
                    _redraw_zone(deck, state, dial)

        elif event_type == DialEventType.PUSH:
            pressed = bool(value)
            logger.info("dial[%d] %s", dial, "PRESSED" if pressed else "released")
            if pressed:
                with deck:
                    if dial == BRIGHTNESS_DIAL_INDEX:
                        state.brightness_percent = DEFAULT_BRIGHTNESS_PERCENT
                        deck.set_brightness(state.brightness_percent)
                    else:
                        state.dial_counters[dial] = 0
                    if deck.is_touch():
                        _redraw_zone(deck, state, dial)

    return on_dial


def make_touchscreen_callback(state: ProbeState):
    """Build the touchscreen callback: tap draws a marker, drag/long just log."""

    def on_touch(
        deck: StreamDeck, event_type: TouchscreenEventType, value: dict
    ) -> None:
        if event_type == TouchscreenEventType.SHORT:
            x, y = value["x"], value["y"]
            logger.info("touch SHORT tap at (%d, %d)", x, y)
            state.marker_x = x
            with deck:
                _paint_full_touchscreen(
                    deck,
                    rendering.render_touchscreen_full(
                        deck,
                        brightness_percent=state.brightness_percent,
                        counters=state.dial_counters,
                        marker_x=state.marker_x,
                    ),
                )

        elif event_type == TouchscreenEventType.LONG:
            logger.info("touch LONG press at (%d, %d)", value["x"], value["y"])

        elif event_type == TouchscreenEventType.DRAG:
            logger.info(
                "touch DRAG from (%d, %d) to (%d, %d)",
                value["x"],
                value["y"],
                value["x_out"],
                value["y_out"],
            )

    return on_touch


def attach_callbacks(deck: StreamDeck, state: ProbeState) -> None:
    """Register callbacks for every control the connected deck actually has.

    Capability-gated: keys always, dials only if the deck has dials, the
    touchscreen only if the deck has one. Absent controls are logged so the
    user knows the skip is deliberate, not a failure.
    """
    deck.set_key_callback(make_key_callback())

    if deck.dial_count() > 0:
        deck.set_dial_callback(make_dial_callback(state))
    else:
        logger.info("no dials on this model -- skipping dial exercise")

    if deck.is_touch():
        deck.set_touchscreen_callback(make_touchscreen_callback(state))
    else:
        logger.info("no touchscreen on this model -- skipping touch exercise")

    if deck.touch_key_count() > 0:
        logger.info(
            "model reports %d touch key(s) -- these surface as regular key events",
            deck.touch_key_count(),
        )
