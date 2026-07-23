"""muxplex sidecar: entry point and hotplug + server-connectivity state machine.

Extends the proven `deck_probe/main.py` hotplug pattern (DEVICE_ABSENT <->
DEVICE_ACTIVE, backoff-on-error after unexpected active-session failures)
with a second layer of state nested inside DEVICE_ACTIVE:

- ACTIVE: deck present, server reachable and authenticated. Poll
  `GET /api/sessions` + `GET /api/state` + `GET /api/settings` every
  `poll_interval` seconds, resolve the server's current view (`.views`
  module -- a port of muxplex's `filter_visible`), optionally reorder it
  attention-first (`.attention`), page it (`.interaction.Pager`, dial 1),
  and render keys/strip from the result -- repainting only what actually
  changed, per key.
- SERVER_UNREACHABLE: deck present, server down/unreachable. Show it on the
  strip, retry with exponential backoff (2s doubling, capped at 30s).
- AUTH_FAILED: deck present, server reachable but rejected the federation
  key (401/403). Distinct from unreachable: logged CRITICAL, shown on the
  strip, retried slowly (every 30s) -- never spins, never proceeds in a
  lesser state.

Unplug at any point drops straight back to DEVICE_ABSENT (server traffic
stops entirely). Ctrl+C/SIGTERM blanks the deck (if present) and exits 0.

Dial 0 cycles the server's (global) `active_view` -- turning locally echoes
the candidate on the strip and debounces the actual `PATCH /api/state`;
pressing jumps straight to "all". Dial 1 pages the current view locally (no
server writes). See `.interaction` for both state machines. All of this
plus the poll loop's own GETs share one `MuxplexClient`, so every actual
HTTP call (poll-loop GETs, a dial-0 commit's PATCH+refresh, a key press's
connect) is serialized through `_ActiveRuntime.client_lock` -- dial/key
callbacks arrive on a device-callback thread, distinct from the poll loop's
thread, and `httpx.Client` concurrency across threads is not a guarantee
worth relying on here.

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

from . import attention, rendering, views
from .client import (
    AuthError,
    MuxplexClient,
    MuxplexError,
    ServerState,
    Session,
    Settings,
    UnreachableError,
)
from .config import Config, ConfigError, load_config
from .device import (
    DeckDevice,
    DeviceManager,
    DeviceProbeError,
    DialEventType,
    TouchscreenEventType,
)
from .interaction import Pager, ViewCycler

logger = logging.getLogger("muxplex_deck")

DEVICE_POLL_SECONDS = 2.0
ABSENT_HEARTBEAT_SECONDS = 30.0
HEALTH_CHECK_TICK_SECONDS = 1.0

INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0
AUTH_RETRY_SECONDS = 30.0

_MAX_VIEW_LABEL_CHARS = 20


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


def _truncate_view(name: str) -> str:
    if len(name) <= _MAX_VIEW_LABEL_CHARS:
        return name
    return name[: _MAX_VIEW_LABEL_CHARS - 1] + "\u2026"


def _build_strip_message(
    *,
    view_label: str,
    turning: bool,
    page: int,
    page_count: int,
    hostname: str,
    total: int,
    active_session: str | None,
) -> str:
    """Compose the touch-strip headline: view (+ live turn echo) + page + host + status."""
    view_part = _truncate_view(view_label)
    if turning:
        view_part = f"\u2192 {view_part}"
    parts = [view_part]
    if page_count > 1:
        parts.append(f"p{page}/{page_count}")
    parts.append(hostname)
    parts.append(f"{total} sessions")
    parts.append(f"ACTIVE: {active_session or 'none'}")
    return " \u00b7 ".join(parts)


def _paint_status_only(deck: DeckDevice, message: str) -> None:
    with deck:
        rendering.paint_blank_keys(deck)
        rendering.paint_status_strip(deck, message)


class _ActiveRuntime:
    """All per-connection mutable state for one active (device+server) session.

    Single home for: the last fetched+ordered session list, dial-driven
    view-cycle/paging state, per-key/strip change-detection caches, and the
    lock serializing every client HTTP call. Constructed fresh in
    `_run_active` for each connection, so a reconnect always starts from a
    clean slate (fresh debounce state, fresh "have we logged the
    no-last_activity_at notice yet" flag, etc.).
    """

    def __init__(
        self, deck: DeckDevice, client: MuxplexClient, hostname: str, sort_mode: str
    ) -> None:
        self.deck = deck
        self.client = client
        self.hostname = hostname
        self.sort_mode = sort_mode

        # Guards every actual HTTP call -- poll-loop GETs, a dial-0 commit's
        # PATCH+refresh, and key-press connects can each originate from a
        # different thread.
        self.client_lock = threading.Lock()
        # Guards the live session/view/page state and the paint-diff caches.
        self.paint_lock = threading.Lock()

        self.view_cycler = ViewCycler()
        self.pager = Pager(page_size=deck.key_count())

        self.ordered: list[Session] = []
        self.active_session: str | None = None
        self.active_view: str = "all"
        self.session_names: list[str] = []  # current page's key-index -> session name

        self.last_key_state: list[object] = [None] * deck.key_count()
        self.last_strip: str | None = None

        self.activity_logged = False
        self.last_seen_active_view: str | None = None

    def invalidate_paint_cache(self) -> None:
        """Force the next `repaint()` to redraw everything.

        Called after a status-only screen (AUTH FAILED / UNREACHABLE) has
        blanked the physical keys out from under our diff cache.
        """
        with self.paint_lock:
            self.last_key_state = [None] * self.deck.key_count()
            self.last_strip = None

    # --- fetch + process ---------------------------------------------------

    def refresh(self) -> None:
        """One GET-sessions/state/settings + process + repaint cycle.

        Raises `AuthError` / `UnreachableError` / `MuxplexError` on failure;
        callers (the poll loop, and a dial-0 commit) handle those.
        """
        with self.client_lock:
            sessions = self.client.get_sessions()
            server_state = self.client.get_state()
            settings = self.client.get_settings()
        self._process(sessions, server_state, settings)

    def _process(
        self, sessions: list[Session], server_state: ServerState, settings: Settings
    ) -> None:
        filtered = views.resolve_view(sessions, settings, server_state.active_view)

        if self.sort_mode == "attention":
            available = attention.activity_available(sessions)
            if not available and not self.activity_logged:
                logger.info(
                    "server does not expose last_activity_at; attention sort "
                    "using bell recency + base order"
                )
                self.activity_logged = True
            ordered = attention.apply_attention_sort(
                filtered, server_state.active_session, activity_available=available
            )
        else:
            ordered = filtered

        view_names = [v.name for v in settings.views]
        self.view_cycler.sync(view_names, server_state.active_view)

        with self.paint_lock:
            if (
                self.last_seen_active_view is not None
                and self.last_seen_active_view != server_state.active_view
            ):
                self.pager.reset()
            self.last_seen_active_view = server_state.active_view

            self.ordered = ordered
            self.active_session = server_state.active_session
            self.active_view = server_state.active_view
            self.pager.set_item_count(len(ordered))

        self.repaint()

    # --- painting ------------------------------------------------------

    def repaint(self) -> None:
        """Repaint keys (page slice, diffed per key) and the strip (diffed as a whole).

        Safe to call frequently and from any thread: a call that changes
        nothing costs a slice + a few tuple comparisons, no device I/O.
        """
        with self.paint_lock:
            start, stop = self.pager.slice_bounds()
            key_count = self.deck.key_count()
            page_sessions = self.ordered[start:stop][:key_count]
            self.session_names = [s.name for s in page_sessions]
            active_session = self.active_session
            turning = self.view_cycler.is_turning()
            view_label = (
                self.view_cycler.candidate_view() if turning else self.active_view
            )
            message = _build_strip_message(
                view_label=view_label,
                turning=turning,
                page=self.pager.page,
                page_count=self.pager.page_count,
                hostname=self.hostname,
                total=len(self.ordered),
                active_session=active_session,
            )
            with self.deck:
                self._paint_keys(page_sessions, active_session)
                if message != self.last_strip:
                    rendering.paint_status_strip(self.deck, message)
                    self.last_strip = message

    def _paint_keys(
        self, page_sessions: list[Session], active_session: str | None
    ) -> None:
        """Paint only the keys whose rendered content actually changed.

        `identity` captures every input that affects a key's pixels (name,
        active flag, bell flag, and the raw snapshot text driving the mini
        terminal preview) as a plain tuple, compared by equality against
        the last-painted identity for that slot. A literal hash isn't
        needed here -- tuple equality is exactly as correct and simpler --
        but it serves the same purpose: skip a repaint (and a JPEG encode)
        for a key whose preview hasn't scrolled since last poll.
        """
        key_count = self.deck.key_count()
        for index in range(key_count):
            session = page_sessions[index] if index < len(page_sessions) else None
            active = session is not None and session.name == active_session
            identity: object = (
                None
                if session is None
                else (
                    session.name,
                    active,
                    session.bell.needs_attention,
                    session.snapshot,
                )
            )
            if self.last_key_state[index] == identity:
                continue
            if session is None:
                self.deck.set_key_image(index, rendering.render_empty_key(self.deck))
            else:
                self.deck.set_key_image(
                    index,
                    rendering.render_session_key(self.deck, session, active=active),
                )
            self.last_key_state[index] = identity

    # --- dial handling ------------------------------------------------

    def handle_view_dial(self, event_type: DialEventType, value: object) -> None:
        if event_type == DialEventType.TURN:
            self.view_cycler.turn(int(value), self._commit_view)  # type: ignore[arg-type]
            self.repaint()
        elif event_type == DialEventType.PUSH and value:
            self.view_cycler.press(self._commit_view)
            self.repaint()

    def _commit_view(self, view: str) -> None:
        """Debounced (or press-immediate) dial-0 commit: PATCH, then refresh fast."""
        logger.info("dial[0] view cycle commit -> %r", view)
        try:
            with self.client_lock:
                self.client.set_active_view(view)
            self.refresh()
        except MuxplexError:
            logger.exception("failed to commit view switch to %r", view)
            message = f"view switch failed: {view}"
            with self.paint_lock, self.deck:
                rendering.paint_status_strip(self.deck, message)
                self.last_strip = message

    def handle_page_dial(self, event_type: DialEventType, value: object) -> None:
        if event_type == DialEventType.TURN:
            self.pager.turn(int(value))  # type: ignore[arg-type]
            self.repaint()
        elif event_type == DialEventType.PUSH and value:
            self.pager.press()
            self.repaint()

    # --- key handling ------------------------------------------------------

    def connect(self, key: int) -> None:
        with self.paint_lock:
            names = list(self.session_names)
        if key >= len(names):
            logger.info("key[%d] pressed (empty slot, ignoring)", key)
            return
        name = names[key]
        logger.info("key[%d] pressed -> connect session %r", key, name)
        try:
            with self.client_lock:
                self.client.connect_session(name)
        except MuxplexError:
            logger.exception("failed to switch to session %r", name)
            with self.deck:
                rendering.paint_status_strip(self.deck, f"switch failed: {name}")
        # The next poll (or dial-driven refresh) re-reads /api/state and
        # repaints the active highlight.


def _make_key_callback(ctx: _ActiveRuntime):
    def on_key(_deck: DeckDevice, key: int, pressed: bool) -> None:
        if pressed:
            ctx.connect(key)

    return on_key


def _make_dial_callback(ctx: _ActiveRuntime):
    def on_dial(
        _deck: DeckDevice, dial: int, event_type: DialEventType, value: object
    ) -> None:
        if dial == 0:
            ctx.handle_view_dial(event_type, value)
        elif dial == 1:
            ctx.handle_page_dial(event_type, value)
        else:
            logger.info("dial[%d] %s %r (unassigned)", dial, event_type, value)

    return on_dial


def _on_touch(_deck: DeckDevice, event_type: TouchscreenEventType, value: dict) -> None:
    logger.info("touch %s %r (unassigned in v1)", event_type, value)


def _run_active(
    deck: DeckDevice,
    client: MuxplexClient,
    shutting_down: threading.Event,
    poll_interval: float,
    hostname: str,
    sort_mode: str,
) -> None:
    """Run one connected-device session against the muxplex server.

    Returns when the device disconnects or shutdown is requested. Server
    errors (unreachable/auth) are handled *inside* this loop -- they never
    propagate up to trigger the outer hotplug recovery path, since they are
    expected, recoverable conditions, not device errors.
    """
    _log_device_info(deck)
    ctx = _ActiveRuntime(deck, client, hostname, sort_mode)
    _paint_status_only(deck, "connecting to muxplex...")

    deck.set_key_callback(_make_key_callback(ctx))
    deck.set_dial_callback(_make_dial_callback(ctx))
    deck.set_touchscreen_callback(_on_touch)

    logger.info(
        "Stream Deck+ active -- polling %s every %.1fs (sort=%s)",
        hostname,
        poll_interval,
        sort_mode,
    )

    backoff = INITIAL_BACKOFF_SECONDS
    shown_error_state: str | None = None

    try:
        while True:
            if not deck.is_open() or not deck.connected():
                logger.warning("Stream Deck+ disconnected")
                return

            try:
                ctx.refresh()
            except AuthError as exc:
                logger.critical(
                    "muxplex auth rejected -- check the federation key file: %s", exc
                )
                if shown_error_state != "auth":
                    _paint_status_only(deck, "AUTH FAILED -- check key file")
                    ctx.invalidate_paint_cache()
                    shown_error_state = "auth"
                if _interruptible_wait(deck, shutting_down, AUTH_RETRY_SECONDS):
                    return
                continue
            except UnreachableError as exc:
                logger.warning(
                    "muxplex unreachable: %s -- retrying in %.0fs", exc, backoff
                )
                if shown_error_state != "unreachable":
                    _paint_status_only(deck, f"{hostname} UNREACHABLE -- retrying")
                    ctx.invalidate_paint_cache()
                    shown_error_state = "unreachable"
                if _interruptible_wait(deck, shutting_down, backoff):
                    return
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except MuxplexError:
                logger.exception(
                    "unexpected muxplex API error; treating as unreachable"
                )
                if shown_error_state != "unreachable":
                    _paint_status_only(deck, f"{hostname} ERROR -- retrying")
                    ctx.invalidate_paint_cache()
                    shown_error_state = "unreachable"
                if _interruptible_wait(deck, shutting_down, backoff):
                    return
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            backoff = INITIAL_BACKOFF_SECONDS
            shown_error_state = None
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
                        deck,
                        client,
                        shutting_down,
                        config.poll_interval,
                        hostname,
                        config.sort,
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
