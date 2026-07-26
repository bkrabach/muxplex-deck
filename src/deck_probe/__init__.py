"""deck_probe: Elgato Stream Deck+ hardware probe.

Proves we can drive every feature of the Stream Deck+ (8 LCD keys, 4 dial
encoders, 800x100 touch strip) and capture every input, including clean
hotplug (unplug/replug) handling with no crash and no zombie threads.

This is the seed of the future muxplex sidecar's device I/O layer -- no
server/network integration lives in this package, device I/O only.
"""

from .main import main

__all__ = ["main"]
__version__ = "0.2.0"
