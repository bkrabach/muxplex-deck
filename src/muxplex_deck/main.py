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

Controls are capability-driven (see `.layout`): on a deck with dials and a
touch strip (Stream Deck+), dial 0 cycles the server's (global)
`active_view` -- turning locally echoes the candidate on the strip and
debounces the actual `PATCH /api/state` -- and dial 1 pages the current
view locally (no server writes); see `.interaction` for both state
machines. On a deck with neither (Original/MK2/XL/Mini), three reserved
keys play those roles instead: VIEW (top-left; shows view + server, tap
opens a paged view picker on the session-slot keys -- VIEW becomes BACK,
PREV/NEXT page the list, tapping a view PATCHes the server-global
`active_view`), PREV/NEXT (bottom corners; paging). All of this
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

import logging
import signal
import threading
import time
from urllib.parse import urlparse

from muxplex_client import (
    AuthError,
    MuxplexClient,
    MuxplexError,
    ServerState,
    Session,
    Settings,
    UnreachableError,
)

from . import attention, focus, interaction, layout, rendering, views
from .config import Config
from .device import (
    DeckDevice,
    DeviceManager,
    DialEventType,
    TouchscreenEventType,
)
from .interaction import Pager, PickerController, PickerMode, ViewCycler
from .statusfile import StatusReporter

logger = logging.getLogger("muxplex_deck")

DEVICE_POLL_SECONDS = 2.0
ABSENT_HEARTBEAT_SECONDS = 30.0
HEALTH_CHECK_TICK_SECONDS = 1.0

INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30.0
AUTH_RETRY_SECONDS = 30.0

# Real hardware powers on at a dim firmware default; the sidecar always
# asserts full brightness itself on every bring-up (fresh connect or
# replug) rather than trusting whatever the deck inherited.
FULL_BRIGHTNESS_PERCENT = 100

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
        # ASCII, not "\u2192" (RIGHTWARDS ARROW): the real device's default
        # PIL font has no glyph for it and renders a .notdef box instead
        # (real-hardware feedback). The middot separator below renders fine.
        view_part = f"> {view_part}"
    parts = [view_part]
    if page_count > 1:
        parts.append(f"p{page}/{page_count}")
    parts.append(hostname)
    parts.append(f"{total} sessions")
    parts.append(f"ACTIVE: {active_session or 'none'}")
    return " \u00b7 ".join(parts)


def _build_picker_strip_message(
    *, kind: str, start: int, total: int, page_size: int
) -> str:
    """Compose the touch-strip headline while a picker (view/page) is open.

    `kind` is "VIEW" or "PAGE". A "start-stop/total" range is only shown
    once there are more options than fit in one window (matching the
    session strip's own "only show page info when there's more than one
    page" convention).
    """
    parts = [f"{kind} PICKER -- tap to choose"]
    if total > page_size:
        first = start + 1
        last = min(start + page_size, total)
        parts.append(f"{first}-{last}/{total}")
    return " \u00b7 ".join(parts)


def _paint_status_only(deck: DeckDevice, message: str, plan: layout.LayoutPlan) -> None:
    """Blank the keys and show `message` wherever this deck can show status.

    Decks with a touch strip get the message there (pre-existing behavior);
    strip-less decks get it word-wrapped onto the VIEW key's position (or
    key 0 on a degenerate grid) -- the log always carries the full detail.
    """
    with deck:
        rendering.paint_blank_keys(deck)
        if plan.use_strip:
            rendering.paint_status_strip(deck, message)
        else:
            status_key = plan.view_key if plan.view_key is not None else 0
            deck.set_key_image(status_key, rendering.render_status_key(deck, message))


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
        self,
        deck: DeckDevice,
        client: MuxplexClient,
        hostname: str,
        sort_mode: str,
        focus_app_name: str = "",
    ) -> None:
        self.deck = deck
        self.client = client
        self.hostname = hostname
        self.sort_mode = sort_mode
        # macOS app name of the local muxplex PWA to bring forward on a
        # key-press session switch; "" disables (see `.focus`).
        self.focus_app_name = focus_app_name

        # Guards every actual HTTP call -- poll-loop GETs, a dial-0 commit's
        # PATCH+refresh, and key-press connects can each originate from a
        # different thread.
        self.client_lock = threading.Lock()
        # Guards the live session/view/page state and the paint-diff caches.
        self.paint_lock = threading.Lock()

        # Capability-driven layout: FULL (Stream Deck+: dials + strip) keeps
        # the pre-existing behavior; REDUCED (Original/MK2/XL/Mini: no
        # dials/strip) reserves VIEW/PREV/NEXT keys instead. See `.layout`.
        self.plan = layout.plan_layout(layout.read_capabilities(deck))

        self.view_cycler = ViewCycler()
        self.pager = Pager(page_size=max(1, self.plan.sessions_per_page))
        self.picker = PickerController()

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
            sessions = self.client.sessions()
            server_state = self.client.state()
            settings = self.client.settings()
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
        """Repaint the keys (session page, or an open picker's options) and the strip.

        Safe to call frequently and from any thread: a call that changes
        nothing costs a slice + a few tuple comparisons, no device I/O.
        """
        if self.picker.mode == PickerMode.NONE:
            self._repaint_sessions()
        else:
            self._repaint_picker(self.picker.mode)

    def _repaint_sessions(self) -> None:
        with self.paint_lock:
            start, stop = self.pager.slice_bounds()
            slots = self.plan.session_slots
            page_sessions = self.ordered[start:stop][: len(slots)]
            self.session_names = [s.name for s in page_sessions]
            active_session = self.active_session
            turning = self.view_cycler.is_turning()
            view_label = (
                self.view_cycler.candidate_view() if turning else self.active_view
            )
            with self.deck:
                self._paint_keys(page_sessions, active_session)
                if self.plan.mode == layout.MODE_REDUCED:
                    self._paint_control_keys(view_label, turning)
                if self.plan.use_strip:
                    message = _build_strip_message(
                        view_label=view_label,
                        turning=turning,
                        page=self.pager.page,
                        page_count=self.pager.page_count,
                        hostname=self.hostname,
                        total=len(self.ordered),
                        active_session=active_session,
                    )
                    if message != self.last_strip:
                        rendering.paint_status_strip(self.deck, message)
                        self.last_strip = message

    def _repaint_picker(self, mode: PickerMode) -> None:
        """Repaint the keys as a picker (view names, or page numbers).

        `mode` is passed in (rather than re-read) so a mode change between
        `repaint()`'s dispatch and here can't leave this method confused
        about which picker it's rendering.

        FULL mode spreads the options across every key (the dial scrolls);
        REDUCED mode shows them on the session-slot keys only, with the
        reserved keys repurposed as BACK / PREV / NEXT (the picker's own
        paging) -- see `_paint_reduced_picker`.
        """
        with self.paint_lock:
            reduced = (
                self.plan.mode == layout.MODE_REDUCED and self.plan.view_key is not None
            )
            page_size = (
                max(1, self.plan.sessions_per_page)
                if reduced
                else self.deck.key_count()
            )
            if mode == PickerMode.VIEW:
                options = self.view_cycler.names()
                current = self.active_view
                kind = "VIEW"
            else:
                options = [str(n) for n in range(1, self.pager.page_count + 1)]
                current = str(self.pager.page)
                kind = "PAGE"

            total = len(options)
            # ticks=0 is a pure re-clamp: keeps the stored window valid if
            # `total` shrank (e.g. a view was deleted) since it was last set.
            start = self.picker.scroll(0, total=total, page_size=page_size)
            window = options[start : start + page_size]
            message = _build_picker_strip_message(
                kind=kind, start=start, total=total, page_size=page_size
            )
            with self.deck:
                if reduced:
                    self._paint_reduced_picker(
                        window, current, start=start, total=total, page_size=page_size
                    )
                else:
                    self._paint_picker_keys(window, current)
                if self.plan.use_strip and message != self.last_strip:
                    rendering.paint_status_strip(self.deck, message)
                    self.last_strip = message

    def _paint_picker_keys(self, options: list[str], current: str) -> None:
        """Paint the picker's key window -- one option label per key, diffed.

        `current` marks whichever option matches today's actual active
        view/page with the same cyan border used for the active session.
        """
        key_count = self.deck.key_count()
        for index in range(key_count):
            label = options[index] if index < len(options) else None
            is_current = label is not None and label == current
            identity: object = None if label is None else ("picker", label, is_current)
            if self.last_key_state[index] == identity:
                continue
            if label is None:
                self.deck.set_key_image(index, rendering.render_empty_key(self.deck))
            else:
                self.deck.set_key_image(
                    index,
                    rendering.render_picker_key(self.deck, label, current=is_current),
                )
            self.last_key_state[index] = identity

    def _paint_reduced_picker(
        self,
        window: list[str],
        current: str,
        *,
        start: int,
        total: int,
        page_size: int,
    ) -> None:
        """Paint the reduced-layout picker: options on session slots, controls reserved.

        The session-slot keys show the current window of options (one view
        name per key, the active one marked with the same cyan border the
        active session tile uses); the reserved keys are repurposed as
        BACK (the VIEW key) and PREV/NEXT (the picker's own paging, with a
        pN/M footer only when there is more than one page -- the same
        convention the session grid's controls use). Same per-key diffing
        as every other paint path.
        """
        for slot, key_index in enumerate(self.plan.session_slots):
            label = window[slot] if slot < len(window) else None
            is_current = label is not None and label == current
            identity: object = None if label is None else ("picker", label, is_current)
            if self.last_key_state[key_index] == identity:
                continue
            if label is None:
                self.deck.set_key_image(
                    key_index, rendering.render_empty_key(self.deck)
                )
            else:
                self.deck.set_key_image(
                    key_index,
                    rendering.render_picker_key(self.deck, label, current=is_current),
                )
            self.last_key_state[key_index] = identity

        page = start // page_size + 1
        page_count = max(1, (total + page_size - 1) // page_size)
        page_text = f"p{page}/{page_count}" if page_count > 1 else ""
        specs: list[tuple[int | None, str, str, str]] = [
            (self.plan.view_key, "VIEW", "< BACK", ""),
            (self.plan.prev_key, "", "< PREV", page_text),
            (self.plan.next_key, "", "NEXT >", page_text),
        ]
        for key_index, title, body, footer in specs:
            if key_index is None:
                continue
            control_identity: object = ("control", title, body, footer)
            if self.last_key_state[key_index] == control_identity:
                continue
            self.deck.set_key_image(
                key_index,
                rendering.render_control_key(
                    self.deck, title=title, body=body, footer=footer
                ),
            )
            self.last_key_state[key_index] = control_identity

    def _paint_keys(
        self, page_sessions: list[Session], active_session: str | None
    ) -> None:
        """Paint only the session-slot keys whose rendered content changed.

        Iterates the plan's `session_slots` (every key in FULL mode; the
        non-reserved keys in REDUCED mode), mapping slot position -> the
        page's session at that position. `identity` captures every input
        that affects a key's pixels (name, active flag, bell flag, and the
        raw snapshot text driving the mini terminal preview) as a plain
        tuple, compared by equality against the last-painted identity for
        that slot. A literal hash isn't needed here -- tuple equality is
        exactly as correct and simpler -- but it serves the same purpose:
        skip a repaint (and a JPEG encode) for a key whose preview hasn't
        scrolled since last poll.
        """
        for slot, key_index in enumerate(self.plan.session_slots):
            session = page_sessions[slot] if slot < len(page_sessions) else None
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
            if self.last_key_state[key_index] == identity:
                continue
            if session is None:
                self.deck.set_key_image(
                    key_index, rendering.render_empty_key(self.deck)
                )
            else:
                self.deck.set_key_image(
                    key_index,
                    rendering.render_session_key(self.deck, session, active=active),
                )
            self.last_key_state[key_index] = identity

    def _paint_control_keys(self, view_label: str, turning: bool) -> None:
        """Paint the reserved VIEW/PREV/NEXT keys (REDUCED mode only), diffed.

        The VIEW key carries what the Deck+'s touch strip showed -- the
        current view name (with the same "> " turn echo the strip used;
        ASCII, because the default PIL font renders arrows as .notdef
        boxes -- real-hardware feedback) plus the server label. PREV/NEXT
        carry a pN/M footer whenever a view has more than one page,
        matching the strip's own "only show page info when it matters"
        convention.
        """
        page = self.pager.page
        page_count = self.pager.page_count
        page_text = f"p{page}/{page_count}" if page_count > 1 else ""
        view_body = f"> {view_label}" if turning else view_label

        specs: list[tuple[int | None, str, str, str]] = [
            (self.plan.view_key, "VIEW", view_body, self.hostname),
            (self.plan.prev_key, "", "< PREV", page_text),
            (self.plan.next_key, "", "NEXT >", page_text),
        ]
        for key_index, title, body, footer in specs:
            if key_index is None:
                continue
            identity: object = ("control", title, body, footer)
            if self.last_key_state[key_index] == identity:
                continue
            self.deck.set_key_image(
                key_index,
                rendering.render_control_key(
                    self.deck, title=title, body=body, footer=footer
                ),
            )
            self.last_key_state[key_index] = identity

    # --- dial handling ------------------------------------------------

    def handle_view_dial(self, event_type: DialEventType, value: object) -> None:
        if event_type == DialEventType.TURN:
            ticks = int(value)  # type: ignore[arg-type]
            if self.picker.mode == PickerMode.VIEW:
                total = len(self.view_cycler.names())
                self.picker.scroll(ticks, total=total, page_size=self.deck.key_count())
            elif self.picker.mode == PickerMode.NONE:
                # Normal-mode turn behavior is unchanged -- only PRESS
                # changes meaning (see `PickerController`'s docstring).
                self.view_cycler.turn(int(value), self._commit_view)  # type: ignore[arg-type]
            # else: PAGE picker is open -- dial 0 isn't its owner, ignore the turn.
            self.repaint()
        elif event_type == DialEventType.PUSH and value:
            logger.info("dial[0] pressed -> %s", self.picker.press_view_dial())
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
            ticks = int(value)  # type: ignore[arg-type]
            if self.picker.mode == PickerMode.PAGE:
                total = self.pager.page_count
                self.picker.scroll(ticks, total=total, page_size=self.deck.key_count())
            elif self.picker.mode == PickerMode.NONE:
                # Normal-mode turn behavior is unchanged -- only PRESS
                # changes meaning (see `PickerController`'s docstring).
                self.pager.turn(int(value))  # type: ignore[arg-type]
            # else: VIEW picker is open -- dial 1 isn't its owner, ignore the turn.
            self.repaint()
        elif event_type == DialEventType.PUSH and value:
            logger.info("dial[1] pressed -> %s", self.picker.press_page_dial())
            self.repaint()

    # --- key handling ------------------------------------------------------

    def handle_key(self, key: int) -> None:
        """Dispatch a physical key press to its role under the layout plan.

        In FULL mode every key is a session slot (`classify_key` maps key N
        to slot N), so this is the pre-existing connect path unchanged. In
        REDUCED mode the reserved VIEW/PREV/NEXT keys perform their control
        action and never connect. Picker mode takes priority: in FULL mode
        (pickers open via dial presses) a tap selects an option; in REDUCED
        mode (the VIEW key opens the view picker) taps dispatch through the
        pure `interaction.handle_picker_key` -- BACK cancels, PREV/NEXT
        page, a view-slot tap selects.
        """
        mode = self.picker.mode
        if mode == PickerMode.VIEW:
            if self.plan.mode == layout.MODE_REDUCED and self.plan.view_key is not None:
                self._handle_reduced_picker_key(key)
            else:
                self._select_view_option(key)
            return
        if mode == PickerMode.PAGE:
            self._select_page_option(key)
            return

        kind, slot = layout.classify_key(self.plan, key)
        if kind == layout.KEY_VIEW:
            # Open the paged view picker (replaces the old tap-to-cycle):
            # the session-slot keys become view choices, VIEW becomes BACK,
            # PREV/NEXT page the list. Selection PATCHes the server-global
            # `active_view` -- same effect a dial-0 pick has on the Deck+.
            self.picker.press_view_dial()
            logger.info("key[%d] VIEW pressed -> view picker opened", key)
            self.repaint()
            return
        if kind == layout.KEY_PREV:
            page = self.pager.turn(-1)
            logger.info("key[%d] PREV pressed -> page %d", key, page)
            self.repaint()
            return
        if kind == layout.KEY_NEXT:
            page = self.pager.turn(1)
            logger.info("key[%d] NEXT pressed -> page %d", key, page)
            self.repaint()
            return

        if slot is None:
            logger.info("key[%d] pressed (unassigned, ignoring)", key)
            return
        self.connect_slot(slot, key)

    def connect_slot(self, slot: int, key: int) -> None:
        """Connect the session shown in `slot` (pressed via physical `key`)."""
        with self.paint_lock:
            names = list(self.session_names)
        if slot >= len(names):
            logger.info("key[%d] pressed (empty slot, ignoring)", key)
            return
        name = names[slot]
        logger.info(
            "key[%d] pressed -> connect session %r (optimistic highlight)", key, name
        )
        # Move the highlight NOW (don't wait for the next poll tick) and run
        # the actual HTTP connect on a background thread -- real-hardware
        # feedback showed the old synchronous-on-the-callback-thread call
        # (server-side ttyd kill+respawn, ~2.6s) blocked further device
        # input and delayed the highlight by up to a full poll interval. The
        # PWA already does this optimistically; this mirrors it.
        with self.paint_lock:
            self.active_session = name
        self.repaint()
        threading.Thread(target=self._do_connect, args=(name,), daemon=True).start()

    def _do_connect(self, name: str) -> None:
        """Background-thread body for a key-press connect (see `connect`).

        On failure, logs loudly and shows it on the strip -- but does not
        try to revert the optimistic highlight itself: the next poll's
        `refresh()` re-reads `/api/state` and repaints whatever the server
        actually has, which self-heals a wrong guess without this method
        needing to know anything about polling.

        Focus fires on EVERY explicit key-press connect, whether or not the
        press actually changes the active session: the physical button is
        the user's request to bring the PWA to the foreground with that
        session showing, and re-pressing the already-active session's key is
        a legitimate way to reacquire the window (e.g. after alt-tabbing
        away on the Mac) -- gating focus on a session CHANGE silently
        dropped that use. Poll-driven repaints / dial actions never reach
        this method at all, so focus still never fires on anything but an
        explicit key press. Focus runs before the connect POST (it's ~100ms
        vs the server's multi-second ttyd respawn, so the window is
        foreground by the time the switch lands) and is best-effort:
        `focus_app` swallows every failure. The connect POST itself is
        unconditional too -- harmless when unchanged: the server already
        short-circuits a same-session connect (no ttyd kill/respawn, ~2ms)
        rather than this method needing to skip it.
        """
        focus.focus_app(self.focus_app_name)
        try:
            with self.client_lock:
                self.client.connect(name)
        except MuxplexError:
            logger.exception("failed to switch to session %r", name)
            with self.paint_lock, self.deck:
                message = f"switch failed: {name}"
                rendering.paint_status_strip(self.deck, message)
                self.last_strip = message

    def _select_view_option(self, key: int) -> None:
        names = self.view_cycler.names()
        start = self.picker.window_start
        index = start + key
        self.picker.exit()
        if index >= len(names):
            logger.info("view picker: key[%d] pressed (empty slot, ignoring)", key)
            self.repaint()
            return
        view = names[index]
        logger.info("view picker: key[%d] selected -> view %r", key, view)
        self._commit_view(view)
        self.repaint()

    def _handle_reduced_picker_key(self, key: int) -> None:
        """Dispatch a key press while the reduced-layout view picker is open.

        The *decision* (cancel/select/page/ignore) is the pure
        `interaction.handle_picker_key`; this method only applies its side
        effects. A selection PATCHes the server-global `active_view` (via
        `_commit_view` -> `MuxplexClient.set_active_view`), so every device
        watching this server -- other decks, the PWA -- follows on its next
        poll, exactly like a session connect.
        """
        kind, slot = layout.classify_key(self.plan, key)
        options = self.view_cycler.names()
        result = interaction.handle_picker_key(
            kind=kind,
            slot=slot,
            options=options,
            window_start=self.picker.window_start,
            page_size=max(1, self.plan.sessions_per_page),
        )
        if result.action == interaction.ACTION_CANCEL:
            self.picker.exit()
            logger.info("view picker: key[%d] BACK -> cancelled", key)
        elif result.action == interaction.ACTION_SELECT and result.view is not None:
            self.picker.exit()
            logger.info("view picker: key[%d] selected -> view %r", key, result.view)
            self._commit_view(result.view)
        elif result.action == interaction.ACTION_PAGE:
            self.picker.set_window(result.window_start)
            logger.info(
                "view picker: key[%d] page -> window start %d",
                key,
                result.window_start,
            )
        else:
            logger.info("view picker: key[%d] pressed (empty slot, ignoring)", key)
        self.repaint()

    def _select_page_option(self, key: int) -> None:
        total = self.pager.page_count
        start = self.picker.window_start
        index = start + key
        self.picker.exit()
        if index >= total:
            logger.info("page picker: key[%d] pressed (empty slot, ignoring)", key)
            self.repaint()
            return
        page = index + 1
        logger.info("page picker: key[%d] selected -> page %d", key, page)
        self.pager.go_to(page)
        self.repaint()


def _make_key_callback(ctx: _ActiveRuntime):
    def on_key(_deck: DeckDevice, key: int, pressed: bool) -> None:
        if pressed:
            ctx.handle_key(key)

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


def _describe_deck_caps(deck: DeckDevice) -> dict | None:
    """Best-effort capability dict for the status file (never fatal).

    Same shape `deck_probe.capabilities.describe_capabilities` produces
    (model, serial, firmware, key_count, key_rows/cols, dial_count,
    has_touchscreen, is_visual, ...) -- both production `DeckDevice`
    backends (`RealDeckDevice`, `EmulatorDevice`) satisfy the wider
    `DeckCapabilitySource` protocol it needs (see
    `test_device_protocol_contract.py`). A failure here must never take
    down the active session -- it only degrades what `status` can show.
    """
    try:
        from deck_probe.capabilities import describe_capabilities

        return describe_capabilities(deck)  # type: ignore[arg-type]
    except Exception:
        logger.exception("failed to describe deck capabilities for status file")
        return None


def _run_active(
    deck: DeckDevice,
    client: MuxplexClient,
    shutting_down: threading.Event,
    poll_interval: float,
    hostname: str,
    sort_mode: str,
    focus_app_name: str,
    reporter: StatusReporter,
) -> None:
    """Run one connected-device session against the muxplex server.

    Returns when the device disconnects or shutdown is requested. Server
    errors (unreachable/auth) are handled *inside* this loop -- they never
    propagate up to trigger the outer hotplug recovery path, since they are
    expected, recoverable conditions, not device errors.
    """
    _log_device_info(deck)
    try:
        deck.set_brightness(FULL_BRIGHTNESS_PERCENT)
    except Exception:
        # Real hardware defaults to a dim power-on brightness; this always
        # asserts full brightness on bring-up (real-hardware feedback) --
        # but a device that just went away mid-open shouldn't be fatal here,
        # the outer loop's is_open()/connected() checks handle that.
        logger.exception("failed to set brightness to %d%%", FULL_BRIGHTNESS_PERCENT)
    ctx = _ActiveRuntime(deck, client, hostname, sort_mode, focus_app_name)
    logger.info("%s", layout.describe_plan(ctx.plan))
    _paint_status_only(deck, "connecting to muxplex...", ctx.plan)

    reporter.update(device_connected=True, device_caps=_describe_deck_caps(deck))

    deck.set_key_callback(_make_key_callback(ctx))
    if ctx.plan.use_dials:
        deck.set_dial_callback(_make_dial_callback(ctx))
    else:
        logger.info("no dials assigned on this model -- view/page controls on keys")
    if ctx.plan.use_strip:
        deck.set_touchscreen_callback(_on_touch)
    else:
        logger.info("no touchscreen on this model -- skipping strip")

    logger.info(
        "Stream Deck active -- polling %s every %.1fs (sort=%s)",
        hostname,
        poll_interval,
        sort_mode,
    )

    backoff = INITIAL_BACKOFF_SECONDS
    shown_error_state: str | None = None

    try:
        while True:
            if not deck.is_open() or not deck.connected():
                logger.warning("Stream Deck disconnected")
                return

            try:
                ctx.refresh()
            except AuthError as exc:
                reporter.update(server_connected=False, last_error=str(exc))
                logger.critical(
                    "muxplex auth rejected -- check the federation key file: %s", exc
                )
                if shown_error_state != "auth":
                    _paint_status_only(deck, "AUTH FAILED -- check key file", ctx.plan)
                    ctx.invalidate_paint_cache()
                    shown_error_state = "auth"
                if _interruptible_wait(deck, shutting_down, AUTH_RETRY_SECONDS):
                    return
                continue
            except UnreachableError as exc:
                reporter.update(server_connected=False, last_error=str(exc))
                logger.warning(
                    "muxplex unreachable: %s -- retrying in %.0fs", exc, backoff
                )
                if shown_error_state != "unreachable":
                    _paint_status_only(
                        deck, f"{hostname} UNREACHABLE -- retrying", ctx.plan
                    )
                    ctx.invalidate_paint_cache()
                    shown_error_state = "unreachable"
                if _interruptible_wait(deck, shutting_down, backoff):
                    return
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except MuxplexError as exc:
                reporter.update(server_connected=False, last_error=str(exc))
                logger.exception(
                    "unexpected muxplex API error; treating as unreachable"
                )
                if shown_error_state != "unreachable":
                    _paint_status_only(deck, f"{hostname} ERROR -- retrying", ctx.plan)
                    ctx.invalidate_paint_cache()
                    shown_error_state = "unreachable"
                if _interruptible_wait(deck, shutting_down, backoff):
                    return
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            backoff = INITIAL_BACKOFF_SECONDS
            shown_error_state = None
            reporter.update(
                server_connected=True,
                last_poll_at=time.time(),
                last_error=None,
                active_session=ctx.active_session,
                active_view=ctx.active_view,
                page=ctx.pager.page,
            )
            if _interruptible_wait(deck, shutting_down, poll_interval):
                return
    finally:
        reporter.update(
            device_connected=False, device_caps=None, server_connected=False
        )
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

    # Published for `muxplex-deck status` to read -- see `.statusfile`'s
    # docstring for why a running sidecar publishes its own status instead
    # of `status` probing the (possibly exclusively-held) device directly.
    reporter = StatusReporter(config.server_url)

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
                reporter.update(
                    device_connected=False, device_caps=None, server_connected=False
                )
                now = time.monotonic()
                if not logged_waiting:
                    logger.info(
                        "waiting for a Stream Deck (polling every %.0fs)...",
                        DEVICE_POLL_SECONDS,
                    )
                    logged_waiting = True
                    last_heartbeat = now
                elif now - last_heartbeat >= ABSENT_HEARTBEAT_SECONDS:
                    logger.info("still waiting for a Stream Deck...")
                    last_heartbeat = now
                shutting_down.wait(DEVICE_POLL_SECONDS)
                continue

            logged_waiting = False
            try:
                deck.open()
            except Exception:
                logger.exception("Failed to open Stream Deck device; will retry")
                shutting_down.wait(DEVICE_POLL_SECONDS)
                continue

            try:
                with MuxplexClient(
                    config.server_url, config.federation_key, ca_file=config.ca_file
                ) as client:
                    _run_active(
                        deck,
                        client,
                        shutting_down,
                        config.poll_interval,
                        hostname,
                        config.sort,
                        config.focus_app,
                        reporter,
                    )
            except Exception:
                logger.exception("Unexpected error during active session; recovering")
                reporter.update(
                    device_connected=False, device_caps=None, server_connected=False
                )
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
    """Legacy direct entry point (`python -m muxplex_deck.main`).

    The actual CLI (subcommands, argument parsing, default-action dispatch)
    lives in `cli.py` now -- delegating here keeps argument parsing
    single-sourced instead of drifting across two copies. The console-script
    entry point (`muxplex-deck`) points at `cli:main` directly; this
    function exists only for anyone still invoking this module by path.
    """
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
