"""Pins the `DeckDevice` <-> `DeckCapabilitySource` protocol contract.

`describe_capabilities()` (deck_probe.capabilities) expects a WIDER surface
than muxplex_deck's own `DeckDevice` protocol historically declared --
`RealDeckDevice` satisfied `DeckDevice` but not `DeckCapabilitySource`,
which is exactly what crashed `muxplex-deck doctor` with a real Stream
Deck+ attached (`AttributeError: 'RealDeckDevice' object has no attribute
'is_visual'`).

This test introspects `DeckCapabilitySource`'s members and asserts every
one of them exists on BOTH production implementations of `DeckDevice`
(`RealDeckDevice`, `EmulatorDevice`). If `deck_probe` widens its needs
again without updating both backends, this test fails in CI instead of a
real user's terminal.
"""

from __future__ import annotations

import inspect

import pytest

from deck_probe.capabilities import DeckCapabilitySource
from muxplex_deck.device_real import RealDeckDevice
from muxplex_deck.emulator import EmulatorDevice


def _protocol_method_names(protocol: type) -> list[str]:
    """Public method names declared directly on a `Protocol` class body."""
    return [
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(value)
    ]


_CAPABILITY_METHODS = _protocol_method_names(DeckCapabilitySource)


class TestDeckCapabilitySourceIntrospection:
    def test_introspection_finds_the_methods_we_expect(self) -> None:
        # Sanity check on the helper itself: if Protocol internals ever
        # change shape such that vars()/inspect.isfunction stops finding
        # members, the parametrized tests below would silently collect
        # zero cases and pass vacuously. Guard against that here.
        assert set(_CAPABILITY_METHODS) >= {
            "deck_type",
            "get_serial_number",
            "get_firmware_version",
            "vendor_id",
            "product_id",
            "key_count",
            "key_layout",
            "key_image_format",
            "dial_count",
            "touch_key_count",
            "is_touch",
            "is_visual",
            "touchscreen_image_format",
        }


@pytest.mark.parametrize("method_name", sorted(_CAPABILITY_METHODS))
class TestProductionBackendsSatisfyDeckCapabilitySource:
    """One assertion per capability method, per backend -- a failure here

    names the exact missing method and the exact backend, rather than a
    single opaque "protocol mismatch" failure.
    """

    def test_real_deck_device_implements(self, method_name: str) -> None:
        assert hasattr(RealDeckDevice, method_name), (
            f"RealDeckDevice is missing {method_name}() required by "
            "deck_probe.capabilities.DeckCapabilitySource -- this is the "
            "exact class of bug that crashed `muxplex-deck doctor` with "
            "real hardware attached."
        )

    def test_emulator_device_implements(self, method_name: str) -> None:
        assert hasattr(EmulatorDevice, method_name), (
            f"EmulatorDevice is missing {method_name}() required by "
            "deck_probe.capabilities.DeckCapabilitySource -- the emulator "
            "must stay in sync with every real DeckDevice backend."
        )
