"""Capability discovery for any Stream Deck model.

The probe adapts to whatever deck is plugged in by querying the library's
capability methods (present on every model -- the base `StreamDeck` class
exposes each subclass's constants). The hard rule: **branch only on
numeric/boolean capability values, never on `deck_type()` strings** --
model names collide (Original vs MK2 are both 15-key/3x5) and new models
would need matrix updates. Capabilities self-describe.

`describe_capabilities` is pure: it takes any object exposing the
capability methods (real deck, fake, emulator) and returns a plain dict,
so the "which deck is this and what can it do" logic is unit-testable
without hardware.
"""

from __future__ import annotations

from typing import Any, Protocol


class DeckCapabilitySource(Protocol):
    """The capability surface every `StreamDeck` subclass exposes.

    Structural typing lets tests substitute fakes and keeps this module
    free of hardware imports. `get_serial_number`/`get_firmware_version`
    require an *open* device; everything else reads class constants.
    """

    def deck_type(self) -> str: ...
    def get_serial_number(self) -> str: ...
    def get_firmware_version(self) -> str: ...
    def vendor_id(self) -> int: ...
    def product_id(self) -> int: ...
    def key_count(self) -> int: ...
    def key_layout(self) -> tuple[int, int]: ...
    def key_image_format(self) -> dict[str, Any]: ...
    def dial_count(self) -> int: ...
    def touch_key_count(self) -> int: ...
    def is_touch(self) -> bool: ...
    def is_visual(self) -> bool: ...
    def touchscreen_image_format(self) -> dict[str, Any]: ...


def describe_capabilities(deck: DeckCapabilitySource) -> dict[str, Any]:
    """Build a capability report dict for the given (open) deck.

    Pure with respect to the deck object: only calls its capability
    methods, performs no device I/O beyond the serial/firmware reads.
    """
    rows, cols = deck.key_layout()
    is_visual = deck.is_visual()
    key_format = deck.key_image_format() if is_visual else None
    has_touchscreen = deck.is_touch()
    touchscreen_size = (
        tuple(deck.touchscreen_image_format()["size"]) if has_touchscreen else None
    )
    return {
        "model": deck.deck_type(),
        "serial": deck.get_serial_number(),
        "firmware": deck.get_firmware_version(),
        "vendor_id": deck.vendor_id(),
        "product_id": deck.product_id(),
        "key_count": deck.key_count(),
        "key_rows": rows,
        "key_cols": cols,
        "key_image_size": tuple(key_format["size"]) if key_format else None,
        "key_image_format": key_format["format"] if key_format else None,
        "dial_count": deck.dial_count(),
        "touch_key_count": deck.touch_key_count(),
        "has_touchscreen": has_touchscreen,
        "touchscreen_size": touchscreen_size,
        "is_visual": is_visual,
    }


def exercises_keys(caps: dict[str, Any]) -> bool:
    """Should the probe paint and monitor LCD keys? (Pedal has none visual.)"""
    return caps["key_count"] > 0 and caps["is_visual"]


def exercises_dials(caps: dict[str, Any]) -> bool:
    """Should the probe attach dial handling? Only when dials exist."""
    return caps["dial_count"] > 0


def exercises_touchscreen(caps: dict[str, Any]) -> bool:
    """Should the probe paint/monitor the touch strip? Only when one exists."""
    return caps["has_touchscreen"]


def exercises_touch_keys(caps: dict[str, Any]) -> bool:
    """Does this model have discrete touch buttons (e.g. Neo's 2)?"""
    return caps["touch_key_count"] > 0


def format_capability_report(caps: dict[str, Any]) -> str:
    """Render the capability dict as a human-readable multi-line block."""
    key_size = caps["key_image_size"]
    key_size_text = f"{key_size[0]}x{key_size[1]}" if key_size else "none (not visual)"
    key_format_text = caps["key_image_format"] or "-"
    if caps["has_touchscreen"]:
        ts_w, ts_h = caps["touchscreen_size"]
        touchscreen_text = f"yes ({ts_w}x{ts_h})"
    else:
        touchscreen_text = "no"
    lines = [
        "deck capabilities:",
        f"  model:            {caps['model']}",
        f"  serial:           {caps['serial']}",
        f"  firmware:         {caps['firmware']}",
        f"  usb id:           {caps['vendor_id']:04x}:{caps['product_id']:04x}",
        f"  keys:             {caps['key_count']} ({caps['key_rows']}x{caps['key_cols']})",
        f"  key image:        {key_size_text} {key_format_text}",
        f"  dials:            {caps['dial_count']}",
        f"  touch keys:       {caps['touch_key_count']}",
        f"  touchscreen:      {touchscreen_text}",
        f"  visual:           {'yes' if caps['is_visual'] else 'no'}",
        "  probe will exercise: "
        + ", ".join(
            part
            for part, active in (
                ("keys", exercises_keys(caps)),
                ("dials", exercises_dials(caps)),
                ("touchscreen", exercises_touchscreen(caps)),
                ("touch-keys", exercises_touch_keys(caps)),
            )
            if active
        ),
    ]
    return "\n".join(lines)
