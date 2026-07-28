"""Stream Deck hardware probe (any model): entry point and hotplug state machine.

The probe is capability-driven: it reports what the connected deck can do
(model, keys, layout, dials, touchscreen, touch keys) and exercises only
the controls that exist -- one probe for the 15-key Original/MK2, the
Plus, the XL, the Mini, the Neo, and whatever ships next. Branching is on
numeric/boolean capabilities (`key_count()`, `dial_count()`, `is_touch()`),
never on `deck_type()` model-name strings.

This state machine (DEVICE_ABSENT <-> DEVICE_ACTIVE) is the seed of the
future muxplex sidecar's core loop, so it's kept clean and self-contained:

- DEVICE_ABSENT: no device found. Re-enumerate every `POLL_INTERVAL_SECONDS`,
  logging once (not spamming every poll), with a periodic heartbeat.
- DEVICE_ACTIVE: device is open and its callbacks are attached. We poll the
  device's own health (`is_open()` / `connected()`) to detect unplug, since
  the underlying reader thread may or may not surface a read error the
  instant the cable is pulled.
- Ctrl+C / SIGTERM: clean shutdown from either state -- reset/blank the
  device if one is connected, close the handle, exit 0.

No server/network integration lives here -- device I/O only.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time

from StreamDeck.DeviceManager import DeviceManager, ProbeError
from StreamDeck.Devices.StreamDeck import StreamDeck
from StreamDeck.Transport.Transport import TransportError

from muxplex_deck import hidapi_win

from . import capabilities, events

logger = logging.getLogger("deck_probe")

POLL_INTERVAL_SECONDS = 2.0
ABSENT_HEARTBEAT_SECONDS = 30.0
ACTIVE_HEALTH_CHECK_SECONDS = 2.0


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _explain_missing_hidapi() -> str:
    return (
        "Could not find the native HIDAPI library required to talk to the Stream Deck.\n"
        "Install it for your platform, then try again:\n"
        "  macOS:          brew install hidapi\n"
        "  Debian/Ubuntu:  sudo apt install libhidapi-libusb0\n"
        "  Windows:        bundled with the 'streamdeck' wheel; if missing, install\n"
        "                  hidapi via your package manager of choice.\n"
    )


def _no_device_guidance() -> str:
    return (
        "No Stream Deck found. Things to check:\n"
        "  - All platforms: close the official Elgato Stream Deck app -- it holds\n"
        "    exclusive HID access, so this probe cannot open the device while it runs.\n"
        "  - Linux (incl. WSL): a udev rule must grant access to Elgato devices\n"
        "    (vendor id 0x0fd9). Create /etc/udev/rules.d/70-streamdeck.rules with:\n"
        '      SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0fd9", MODE="0666"\n'
        "    then: sudo udevadm control --reload-rules && replug the device.\n"
        "  - WSL specifically: USB devices are not visible until attached from\n"
        "    Windows via usbipd -- in an admin PowerShell:\n"
        "      usbipd list                        (find the Stream Deck's BUSID)\n"
        "      usbipd bind --busid <BUSID>        (first time only)\n"
        "      usbipd attach --wsl --busid <BUSID>\n"
        "    (and remember the Elgato app on the Windows side must be closed).\n"
    )


def _find_deck(manager: DeviceManager) -> StreamDeck | None:
    decks = manager.enumerate()
    return decks[0] if decks else None


def _log_device_info(deck: StreamDeck) -> None:
    """Print the capability report: which deck this is and what it can do."""
    for line in capabilities.format_capability_report(
        capabilities.describe_capabilities(deck)
    ).splitlines():
        logger.info("%s", line)


def _safe_close(deck: StreamDeck) -> None:
    """Reset and close the device, swallowing (but logging) any I/O errors.

    Used both for clean Ctrl+C shutdown and for recovering after an unplug
    or unexpected error -- either way we must not leave a half-open handle
    or a zombie reader thread behind.
    """
    try:
        if deck.is_open():
            deck.reset()
    except TransportError:
        pass
    except Exception:
        logger.exception("Unexpected error while resetting deck during close")

    try:
        deck.close()
    except Exception:
        logger.exception("Unexpected error while closing deck")


def _run_active(deck: StreamDeck, shutting_down: threading.Event) -> None:
    """Run one connected-device session until it disconnects or shutdown is requested."""
    _log_device_info(deck)
    state = events.paint_initial_state(deck)
    events.attach_callbacks(deck, state)
    logger.info("%s active -- waiting for input (Ctrl+C to exit)", deck.deck_type())

    try:
        while True:
            if shutting_down.wait(ACTIVE_HEALTH_CHECK_SECONDS):
                return
            if not deck.is_open() or not deck.connected():
                logger.warning("Stream Deck disconnected")
                return
    finally:
        _safe_close(deck)


def _install_signal_handler() -> threading.Event:
    shutting_down = threading.Event()

    def handler(signum: int, frame: object) -> None:
        shutting_down.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    return shutting_down


def run() -> int:
    _configure_logging()

    # No-op on non-Windows; on Windows this must run BEFORE DeviceManager()
    # ever imports/loads the native library -- see hidapi_win's module
    # docstring for why both of its mechanisms are required.
    hidapi_win.ensure_hidapi()
    try:
        manager = DeviceManager()
    except ProbeError:
        print(_explain_missing_hidapi(), file=sys.stderr)
        return 1

    shutting_down = _install_signal_handler()
    logged_waiting = False
    guidance_shown = False
    last_heartbeat = 0.0

    logger.info("deck-probe starting")
    try:
        while not shutting_down.is_set():
            try:
                deck = _find_deck(manager)
            except Exception:
                logger.exception(
                    "Unexpected error while enumerating Stream Deck devices; will retry"
                )
                shutting_down.wait(POLL_INTERVAL_SECONDS)
                continue

            if deck is None:
                now = time.monotonic()
                if not logged_waiting:
                    if not guidance_shown:
                        for line in _no_device_guidance().splitlines():
                            logger.info("%s", line)
                        guidance_shown = True
                    logger.info(
                        "waiting for a Stream Deck (polling every %.0fs)...",
                        POLL_INTERVAL_SECONDS,
                    )
                    logged_waiting = True
                    last_heartbeat = now
                elif now - last_heartbeat >= ABSENT_HEARTBEAT_SECONDS:
                    logger.info("still waiting for a Stream Deck...")
                    last_heartbeat = now
                shutting_down.wait(POLL_INTERVAL_SECONDS)
                continue

            logged_waiting = False
            try:
                deck.open()
            except Exception:
                logger.exception("Failed to open Stream Deck device; will retry")
                shutting_down.wait(POLL_INTERVAL_SECONDS)
                continue

            try:
                _run_active(deck, shutting_down)
            except Exception:
                logger.exception("Unexpected error during active session; recovering")
                _safe_close(deck)
                # Back off before re-enumerating: without this, a deterministic
                # error during session setup (e.g. a bad paint call) spins the
                # connect/fail/reset cycle at full speed -- flooding the log and
                # strobing the device.
                shutting_down.wait(POLL_INTERVAL_SECONDS)
    finally:
        logger.info("deck-probe shutting down")

    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
