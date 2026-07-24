"""Capability-driven probe tests -- no hardware required.

Two fake decks (a 15-key/3x5/no-dial/no-touch Original-class deck and a
Stream Deck+ class deck) exercise `describe_capabilities`, the gating
predicates, the report formatter, and the capability-gated I/O paths
(`paint_initial_state` / `attach_callbacks`) that decide what the probe
exercises on a given model.
"""

from __future__ import annotations

from typing import Any, Self

from deck_probe import capabilities, events


class FakeDeck:
    """Records I/O calls; capability values are injected per model."""

    def __init__(
        self,
        *,
        model: str,
        key_count: int,
        key_layout: tuple[int, int],
        key_size: tuple[int, int],
        dial_count: int = 0,
        touch_key_count: int = 0,
        is_touch: bool = False,
        touchscreen_size: tuple[int, int] = (0, 0),
        is_visual: bool = True,
    ) -> None:
        self._model = model
        self._key_count = key_count
        self._key_layout = key_layout
        self._key_size = key_size
        self._dial_count = dial_count
        self._touch_key_count = touch_key_count
        self._is_touch = is_touch
        self._touchscreen_size = touchscreen_size
        self._is_visual = is_visual
        # Recorded I/O
        self.key_images: dict[int, bytes] = {}
        self.touchscreen_painted = False
        self.brightness: int | None = None
        self.callbacks: set[str] = set()

    # --- capability surface (mirrors StreamDeck base class) ---
    def deck_type(self) -> str:
        return self._model

    def get_serial_number(self) -> str:
        return "FAKE123"

    def get_firmware_version(self) -> str:
        return "1.0.0"

    def vendor_id(self) -> int:
        return 0x0FD9

    def product_id(self) -> int:
        return 0x0060

    def key_count(self) -> int:
        return self._key_count

    def key_layout(self) -> tuple[int, int]:
        return self._key_layout

    def key_image_format(self) -> dict[str, Any]:
        return {
            "size": self._key_size,
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    def dial_count(self) -> int:
        return self._dial_count

    def touch_key_count(self) -> int:
        return self._touch_key_count

    def is_touch(self) -> bool:
        return self._is_touch

    def is_visual(self) -> bool:
        return self._is_visual

    def touchscreen_image_format(self) -> dict[str, Any]:
        return {
            "size": self._touchscreen_size,
            "format": "JPEG",
            "flip": (False, False),
            "rotation": 0,
        }

    # --- device I/O surface used by events.py ---
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def set_brightness(self, percent: int) -> None:
        self.brightness = percent

    def set_key_image(self, key: int, image: bytes) -> None:
        self.key_images[key] = image

    def set_touchscreen_image(
        self, image: bytes, x: int, y: int, width: int, height: int
    ) -> None:
        assert width > 0 and height > 0, "full-strip paint requires explicit region"
        self.touchscreen_painted = True

    def set_key_callback(self, cb: object) -> None:
        self.callbacks.add("key")

    def set_dial_callback(self, cb: object) -> None:
        self.callbacks.add("dial")

    def set_touchscreen_callback(self, cb: object) -> None:
        self.callbacks.add("touchscreen")


def make_15key() -> FakeDeck:
    """Stream Deck Original/MK2 class: 15 keys, 3x5, 72x72, no dials/touch."""
    return FakeDeck(
        model="Stream Deck Original",
        key_count=15,
        key_layout=(3, 5),
        key_size=(72, 72),
    )


def make_plus() -> FakeDeck:
    """Stream Deck+ class: 8 keys, 2x4, 120x120, 4 dials, 800x100 touchscreen."""
    return FakeDeck(
        model="Stream Deck +",
        key_count=8,
        key_layout=(2, 4),
        key_size=(120, 120),
        dial_count=4,
        is_touch=True,
        touchscreen_size=(800, 100),
    )


class TestDescribeCapabilities:
    def test_15key_report(self) -> None:
        caps = capabilities.describe_capabilities(make_15key())
        assert caps["model"] == "Stream Deck Original"
        assert caps["key_count"] == 15
        assert (caps["key_rows"], caps["key_cols"]) == (3, 5)
        assert caps["key_image_size"] == (72, 72)
        assert caps["dial_count"] == 0
        assert caps["touch_key_count"] == 0
        assert caps["has_touchscreen"] is False
        assert caps["touchscreen_size"] is None
        assert caps["is_visual"] is True

    def test_plus_report(self) -> None:
        caps = capabilities.describe_capabilities(make_plus())
        assert caps["model"] == "Stream Deck +"
        assert caps["key_count"] == 8
        assert (caps["key_rows"], caps["key_cols"]) == (2, 4)
        assert caps["key_image_size"] == (120, 120)
        assert caps["dial_count"] == 4
        assert caps["has_touchscreen"] is True
        assert caps["touchscreen_size"] == (800, 100)

    def test_serial_and_firmware_included(self) -> None:
        caps = capabilities.describe_capabilities(make_15key())
        assert caps["serial"] == "FAKE123"
        assert caps["firmware"] == "1.0.0"
        assert caps["vendor_id"] == 0x0FD9


class TestGatingPredicates:
    def test_15key_gates(self) -> None:
        caps = capabilities.describe_capabilities(make_15key())
        assert capabilities.exercises_keys(caps) is True
        assert capabilities.exercises_dials(caps) is False
        assert capabilities.exercises_touchscreen(caps) is False
        assert capabilities.exercises_touch_keys(caps) is False

    def test_plus_gates(self) -> None:
        caps = capabilities.describe_capabilities(make_plus())
        assert capabilities.exercises_keys(caps) is True
        assert capabilities.exercises_dials(caps) is True
        assert capabilities.exercises_touchscreen(caps) is True
        assert capabilities.exercises_touch_keys(caps) is False

    def test_neo_style_touch_keys(self) -> None:
        neo = FakeDeck(
            model="Stream Deck Neo",
            key_count=8,
            key_layout=(2, 4),
            key_size=(96, 96),
            touch_key_count=2,
        )
        caps = capabilities.describe_capabilities(neo)
        assert capabilities.exercises_touch_keys(caps) is True
        assert capabilities.exercises_dials(caps) is False
        assert capabilities.exercises_touchscreen(caps) is False


class TestFormatReport:
    def test_15key_text(self) -> None:
        report = capabilities.format_capability_report(
            capabilities.describe_capabilities(make_15key())
        )
        assert "Stream Deck Original" in report
        assert "15 (3x5)" in report
        assert "72x72" in report
        assert "dials:            0" in report
        assert "touchscreen:      no" in report
        assert "probe will exercise: keys" in report

    def test_plus_text(self) -> None:
        report = capabilities.format_capability_report(
            capabilities.describe_capabilities(make_plus())
        )
        assert "8 (2x4)" in report
        assert "120x120" in report
        assert "touchscreen:      yes (800x100)" in report
        assert "keys, dials, touchscreen" in report


class TestAdaptiveIO:
    """The capability-gated I/O paths in events.py, driven by the fakes."""

    def test_15key_paints_all_keys_at_72px_and_skips_touchscreen(self) -> None:
        deck = make_15key()
        state = events.paint_initial_state(deck)  # type: ignore[arg-type]
        assert sorted(deck.key_images) == list(range(15))
        assert deck.touchscreen_painted is False
        assert deck.brightness == events.DEFAULT_BRIGHTNESS_PERCENT
        assert state.dial_counters == []

    def test_plus_paints_8_keys_and_touchscreen(self) -> None:
        deck = make_plus()
        state = events.paint_initial_state(deck)  # type: ignore[arg-type]
        assert sorted(deck.key_images) == list(range(8))
        assert deck.touchscreen_painted is True
        assert state.dial_counters == [0, 0, 0, 0]

    def test_15key_attaches_only_key_callback(self) -> None:
        deck = make_15key()
        events.attach_callbacks(deck, events.ProbeState())  # type: ignore[arg-type]
        assert deck.callbacks == {"key"}

    def test_plus_attaches_all_callbacks(self) -> None:
        deck = make_plus()
        events.attach_callbacks(deck, events.ProbeState(dial_counters=[0] * 4))  # type: ignore[arg-type]
        assert deck.callbacks == {"key", "dial", "touchscreen"}
