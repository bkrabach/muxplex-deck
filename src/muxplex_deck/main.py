"""muxplex sidecar: entry point and hotplug + server-connectivity state machine.

Extends the proven `deck_probe/main.py` hotplug pattern (DEVICE_ABSENT <->
DEVICE_ACTIVE, backoff-on-error after unexpected active-session failures)
with a second layer of state nested inside DEVICE_ACTIVE:

- ACTIVE: deck present, server reachable and authenticated. Poll
  `GET /api/sessions` + `GET /api/state` + `GET /api/settings` every
  `poll_interval` seconds, resolve the server's current view (`.views`
  module -- a port of muxplex's `filter_visible`) and render keys/strip from
  the result -- but only repaint when render-relevant state changes.
- SERVER_UNREACHABLE: deck present, server down/unreachable. Show it on the
  strip, retry with exponential backoff (2s doubling, capped at 30s).
- AUTH_FAILED: deck present, server reachable but rejected the federation
  key (401/403). Distinct from unreachable: logged CRITICAL, shown on the
  strip, retried slowly (every 30s) -- never spins, never proceeds in a
  lesser state.

Unplug at any point drops straight back to DEVICE_ABSENT (server traffic
stops entirely). Ctrl+C/SIGTERM blanks the deck (if present) and exits 0.

This module depends only on the `DeckDevice` / `DeviceManager` protocols in
`.device` -- never on the `streamdeck` library or hidapi directly. Which
backend actually implements those protocols (real hardware via
`device_real.py`, or the in-process `emulator.py`) is chosen once in
`main()` based on `--emulator`, and everything below that point is
backend-agnostic.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from urllib.parse import urlparse

from . import rendering, views
from .client import AuthError, MuxplexClient, MuxplexError, Session, UnreachableError
from .config import Config, ConfigError, load_config
from .device import (
    DeckDevice,
    DeviceManager,
    DeviceProbeError,
    DialEventType,
    TouchscreenEventType,
)

logger = logging.getLogger("muxplex_deck")

DEVICE_POLL_SECONDS = 2.0
ABSENT_HEARTBEAT_SECONDS = 30.0
HEALTH_CHECK_TICK_SECONDS = 1.0

INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0
AUTH_RETRY_SECONDS = 30.0

# Render-key sentinels for the non-session states -- any tuple starting with
# one of these strings is distinct from an "active" render key, so a state
# transition always triggers a repaint.
_UNREACHABLE_KEY = ("unreachable",)
_AUTH_FAILED_KEY = ("auth_failed",)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def _find_deck(manager: DeviceManager) -> DeckDevice | None:
    return manager.find_device()


def _log_device_info(deck: DeckDevice) -> None:
    logger.info("connected: %s", deck.deck_type())
    logger.info("  serial number:    %s", deck.get_serial_number())
    logger.info("  firmware version: %s", deck.get_firmware_version())
    logger.info("  key count:        %d", deck.key_count())


def _safe_close(deck: DeckDevice) -> None:
    """Reset and close the device, swallowing (but logging) any I/O errors.

    Backend-specific "this is an expected disconnect error, not a bug"
    exceptions (e.g. the real backend's `TransportError`) are swallowed
    *inside* the backend's `reset()`/`close()` -- this function only needs
    to guard against genuinely unexpected failures, from either backend.
    """
    try:
        if deck.is_open():
            deck.reset()
    except Exception:
        logger.exception("Unexpected error while resetting deck during close")

    try:
        deck.close()
    except Exception:
        logger.exception("Unexpected error while closing deck")


def _interruptible_wait(
    deck: DeckDevice, shutting_down: threading.Event, seconds: float
) -> bool:
    """Wait up to `seconds`, checking shutdown and device-health every ~1s.

    Returns True if the caller should abandon this active session (shutdown
    requested, or the device went away) rather than waiting out the full
    duration -- keeps unplug/Ctrl+C responsive even during a 30s backoff.
    """
    remaining = seconds
    while remaining > 0:
        step = min(HEALTH_CHECK_TICK_SECONDS, remaining)
        if shutting_down.wait(step):
            return True
        if not deck.is_open() or not deck.connected():
            return True
        remaining -= step
    return False


_MAX_VIEW_LABEL_CHARS = 20


def _truncate_view(name: str) -> str:
    if len(name) <= _MAX_VIEW_LABEL_CHARS:
        return name
    return name[: _MAX_VIEW_LABEL_CHARS - 1] + "\u2026"


def _session_render_key(
    view: str,
    sort_order: str,
    filtered_sessions: list[Session],
    active_session: str | None,
) -> tuple:
    return (
        "active",
        view,
        sort_order,
        tuple((s.name, s.bell.needs_attention) for s in filtered_sessions),
        active_session,
    )


def _paint_sessions(
    deck: DeckDevice,
    filtered_sessions: list[Session],
    active_session: str | None,
    hostname: str,
    view: str,
) -> None:
    """Paint keys from the view-filtered session list; strip leads with the view name.

    `filtered_sessions` is the full result of `views.resolve_view` (may exceed
    `deck.key_count()`) so the strip's session count reflects the whole view,
    matching the pre-view-aware behavior of showing the true total rather than
    just what fits on the keys.
    """
    key_count = deck.key_count()
    pairs = [(s.name, s.bell.needs_attention) for s in filtered_sessions[:key_count]]
    with deck:
        rendering.paint_sessions(deck, pairs, active_session)
        strip_message = (
            f"{_truncate_view(view)} \u00b7 {hostname} \u00b7 "
            f"{len(filtered_sessions)} sessions \u00b7 ACTIVE: {active_session or 'none'}"
        )
        rendering.paint_status_strip(deck, strip_message)


def _paint_status_only(deck: DeckDevice, message: str) -> None:
    with deck:
        rendering.paint_blank_keys(deck)
        rendering.paint_status_strip(deck, message)


def _make_key_callback(
    client: MuxplexClient, deck: DeckDevice, session_names: list[str]
):
    def on_key(_deck: DeckDevice, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key >= len(session_names):
            logger.info("key[%d] pressed (empty slot, ignoring)", key)
            return
        name = session_names[key]
        logger.info("key[%d] pressed -> connect session %r", key, name)
        try:
            client.connect_session(name)
        except MuxplexError:
            logger.exception("failed to switch to session %r", name)
            with deck:
                rendering.paint_status_strip(deck, f"switch failed: {name}")
        # The next poll tick re-reads /api/state and repaints the highlight.

    return on_key


def _on_dial(
    _deck: DeckDevice, dial: int, event_type: DialEventType, value: object
) -> None:
    logger.info("dial[%d] %s %r (unassigned in v1)", dial, event_type, value)


def _on_touch(_deck: DeckDevice, event_type: TouchscreenEventType, value: dict) -> None:
    logger.info("touch %s %r (unassigned in v1)", event_type, value)


def _run_active(
    deck: DeckDevice,
    client: MuxplexClient,
    shutting_down: threading.Event,
    poll_interval: float,
    hostname: str,
) -> None:
    """Run one connected-device session against the muxplex server.

    Returns when the device disconnects or shutdown is requested. Server
    errors (unreachable/auth) are handled *inside* this loop -- they never
    propagate up to trigger the outer hotplug recovery path, since they are
    expected, recoverable conditions, not device errors.
    """
    _log_device_info(deck)
    session_names: list[str] = []
    _paint_status_only(deck, "connecting to muxplex...")

    deck.set_key_callback(_make_key_callback(client, deck, session_names))
    deck.set_dial_callback(_on_dial)
    deck.set_touchscreen_callback(_on_touch)

    logger.info(
        "Stream Deck+ active -- polling %s every %.1fs", hostname, poll_interval
    )

    last_render_key: tuple | None = None
    backoff = INITIAL_BACKOFF_SECONDS

    try:
        while True:
            if not deck.is_open() or not deck.connected():
                logger.warning("Stream Deck+ disconnected")
                return

            try:
                sessions = client.get_sessions()
                server_state = client.get_state()
                settings = client.get_settings()
            except AuthError as exc:
                logger.critical(
                    "muxplex auth rejected -- check the federation key file: %s", exc
                )
                if last_render_key != _AUTH_FAILED_KEY:
                    _paint_status_only(deck, "AUTH FAILED -- check key file")
                    last_render_key = _AUTH_FAILED_KEY
                if _interruptible_wait(deck, shutting_down, AUTH_RETRY_SECONDS):
                    return
                continue
            except UnreachableError as exc:
                logger.warning(
                    "muxplex unreachable: %s -- retrying in %.0fs", exc, backoff
                )
                if last_render_key != _UNREACHABLE_KEY:
                    _paint_status_only(deck, f"{hostname} UNREACHABLE -- retrying")
                    last_render_key = _UNREACHABLE_KEY
                if _interruptible_wait(deck, shutting_down, backoff):
                    return
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except MuxplexError:
                logger.exception(
                    "unexpected muxplex API error; treating as unreachable"
                )
                if last_render_key != _UNREACHABLE_KEY:
                    _paint_status_only(deck, f"{hostname} ERROR -- retrying")
                    last_render_key = _UNREACHABLE_KEY
                if _interruptible_wait(deck, shutting_down, backoff):
                    return
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            backoff = INITIAL_BACKOFF_SECONDS
            filtered = views.resolve_view(sessions, settings, server_state.active_view)
            session_names[:] = [s.name for s in filtered[: deck.key_count()]]
            render_key = _session_render_key(
                server_state.active_view,
                settings.sort_order,
                filtered,
                server_state.active_session,
            )
            if render_key != last_render_key:
                _paint_sessions(
                    deck,
                    filtered,
                    server_state.active_session,
                    hostname,
                    server_state.active_view,
                )
                last_render_key = render_key

            if _interruptible_wait(deck, shutting_down, poll_interval):
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


def run(config: Config, manager: DeviceManager) -> int:
    _configure_logging()

    shutting_down = _install_signal_handler()
    hostname = urlparse(config.server_url).hostname or config.server_url
    logged_waiting = False
    last_heartbeat = 0.0

    logger.info("muxplex-deck starting (server=%s)", config.server_url)
    try:
        while not shutting_down.is_set():
            try:
                deck = _find_deck(manager)
            except Exception:
                logger.exception(
                    "Unexpected error while enumerating Stream Deck devices; will retry"
                )
                shutting_down.wait(DEVICE_POLL_SECONDS)
                continue

            if deck is None:
                now = time.monotonic()
                if not logged_waiting:
                    logger.info(
                        "waiting for Stream Deck+ (polling every %.0fs)...",
                        DEVICE_POLL_SECONDS,
                    )
                    logged_waiting = True
                    last_heartbeat = now
                elif now - last_heartbeat >= ABSENT_HEARTBEAT_SECONDS:
                    logger.info("still waiting for Stream Deck+...")
                    last_heartbeat = now
                shutting_down.wait(DEVICE_POLL_SECONDS)
                continue

            logged_waiting = False
            try:
                deck.open()
            except Exception:
                logger.exception("Failed to open Stream Deck+ device; will retry")
                shutting_down.wait(DEVICE_POLL_SECONDS)
                continue

            try:
                with MuxplexClient(
                    config.server_url, config.federation_key, config.ca_file
                ) as client:
                    _run_active(
                        deck, client, shutting_down, config.poll_interval, hostname
                    )
            except Exception:
                logger.exception("Unexpected error during active session; recovering")
                _safe_close(deck)
                shutting_down.wait(DEVICE_POLL_SECONDS)
    finally:
        logger.info("muxplex-deck shutting down")

    return 0


def _build_manager(*, emulator: bool, emulator_port: int) -> DeviceManager:
    """Construct the backend's device manager -- the only backend-selection point.

    Real and emulator backends are imported lazily, here, so choosing
    `--emulator` never imports (and thus never risks constructing) the
    hidapi-dependent real backend, and vice versa.
    """
    if emulator:
        from .emulator import EmulatorDeviceManager

        return EmulatorDeviceManager(emulator_port)

    from .device_real import RealDeviceManager

    return RealDeviceManager()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="muxplex-deck",
        description="Drive an Elgato Stream Deck+ against a muxplex server.",
    )
    parser.add_argument(
        "--config",
        help="Path to config JSON file (overrides MUXPLEX_DECK_CONFIG and the default "
        "~/.config/muxplex-deck/config.json)",
    )
    parser.add_argument(
        "--emulator",
        action="store_true",
        help="Run against the in-process Stream Deck+ emulator (localhost web UI) "
        "instead of real hardware -- no device, no hidapi required.",
    )
    parser.add_argument(
        "--emulator-port",
        type=int,
        default=8484,
        help="Port for the emulator's web UI (default: 8484). Ignored without --emulator.",
    )
    args = parser.parse_args()

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    try:
        manager = _build_manager(
            emulator=args.emulator, emulator_port=args.emulator_port
        )
    except DeviceProbeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    sys.exit(run(config, manager))


if __name__ == "__main__":
    main()
