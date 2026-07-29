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
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
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
from . import config as config_mod
from . import controls as controls_mod
from .config import Config
from .device import (
    DeckDevice,
    DeviceManager,
    DialEventType,
    TouchscreenEventType,
)
from .interaction import Pager, PickerController, PickerMode, ViewCycler
from .singleton import InstanceLock, InstanceLockError, default_lock_path
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

# `brightness_up`/`brightness_down`/`brightness_cycle` step size and floor.
# The floor is 10, not 0: a user walking `brightness_down` to a black
# screen would have no way to see which key restores it -- see
# docs/CONTROL_MAPPING_DESIGN.md §2.5. 0% remains reachable
# programmatically (e.g. a future `display_toggle`); it just isn't
# reachable by holding down a bound control.
BRIGHTNESS_STEP_PERCENT = 10
BRIGHTNESS_FLOOR_PERCENT = 10

_MAX_VIEW_LABEL_CHARS = 20

# Static (title, body) display for control-key actions whose label never
# changes at runtime -- everything else (view_picker, page_picker,
# page_prev, page_next) carries live state and is special-cased in
# `_control_key_display`. Relative-only actions (view_cycle, page_cycle,
# brightness_cycle) never appear here: Gate 1 (config.py) rejects them on
# any address but `dial.N.turn`, so they can never be the resolved action
# for a `key.N` or `dial.N.push` control needing a key-paint spec.
_STATIC_CONTROL_LABELS: dict[str, tuple[str, str]] = {
    "view_all": ("VIEW", "ALL"),
    "page_first": ("PAGE", "FIRST"),
    "page_last": ("PAGE", "LAST"),
    "view_prev": ("VIEW", "< PREV"),
    "view_next": ("VIEW", "NEXT >"),
    "focus_app": ("", "FOCUS"),
    "refresh_now": ("", "REFRESH"),
    "toggle_last": ("", "TOGGLE"),
    "brightness_up": ("BRIGHT", "+"),
    "brightness_down": ("BRIGHT", "-"),
}


def _control_key_display(
    action: str, *, view_label: str, turning: bool, hostname: str, page_text: str
) -> tuple[str, str, str]:
    """(title, body, footer) for a non-"session"/"none" control key's paint.

    `view_picker`/`page_picker`/`page_prev`/`page_next` carry live state
    (the current view name, or the page footer) and are special-cased;
    everything else in the catalog has a fixed label from
    `_STATIC_CONTROL_LABELS`.
    """
    if action == "view_picker":
        body = f"> {view_label}" if turning else view_label
        return "VIEW", body, hostname
    if action == "page_picker":
        return "PAGE", "PAGE", page_text
    if action == "page_prev":
        return "", "< PREV", page_text
    if action == "page_next":
        return "", "NEXT >", page_text
    title, body = _STATIC_CONTROL_LABELS.get(action, ("", action))
    return title, body, ""


def _build_log_file_handler(log_file: Path) -> logging.Handler:
    """Best-effort file handler for `log_file`. Never raises.

    Tries a rotating handler first, then a plain (non-rotating) file
    handler, then falls back to stderr (if present) or a `NullHandler`.
    Opening the log file failing must never take the whole *process*
    down: under `pythonw.exe` (Task Scheduler, no console -- see
    WINDOWS_NATIVE_SPEC.md section 1.5) an uncaught exception here means
    the sidecar dies with NO diagnostic anywhere -- no console, no log
    file, nothing -- which is strictly worse than simply missing a log.
    Before this fallback existed, this construction was unguarded, so
    any failure to open the file (permission error, disk full, a
    transient lock from a concurrent writer) crashed `run()` before a
    single line could be logged anywhere.
    """
    from logging.handlers import RotatingFileHandler

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        return RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        pass
    try:
        return logging.FileHandler(log_file, encoding="utf-8")
    except OSError:
        pass
    if sys.stderr is not None:
        return logging.StreamHandler(sys.stderr)
    return logging.NullHandler()


def _configure_logging(log_file: Path | None = None) -> None:
    """Configure logging, optionally to a rotating file instead of stderr.

    `log_file` is required in practice on Windows under Task Scheduler:
    `pythonw.exe` (the GUI-subsystem interpreter `service._resolve_pythonw()`
    resolves to -- see WINDOWS_NATIVE_SPEC.md section 1.5) leaves
    `sys.stdout`/`sys.stderr` as `None`, and `logging.StreamHandler()` around
    a `None` stream would fail. `cli.py`'s `--log-file` flag (all platforms,
    default `None`) is what plumbs a path down to here; on macOS/Linux this
    stays `None` and behavior is byte-for-byte unchanged from before this
    parameter existed.
    """
    if log_file is not None:
        handler = _build_log_file_handler(log_file)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler])
        return
    if sys.stderr is None:
        # Defensive: a console-less launch with no --log-file must not
        # crash trying to build a StreamHandler around a None stream.
        logging.basicConfig(level=logging.INFO, handlers=[logging.NullHandler()])
        return
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


def _shutdown_cleanup(deck: DeckDevice | None) -> None:
    """Blank the Stream Deck's screen on the way out of `_run()`.

    Real-world report: `muxplex-deck service stop` stopped the process, but
    the deck's LCD keys kept showing whatever session icons were painted at
    the moment of shutdown -- indefinitely, since nothing ever repaints a
    physically-powered deck once its only driver has exited. `reset()` is
    the right primitive for this (not a hand-painted blank image per key):
    per the `streamdeck` library's own contract ("Resets the StreamDeck,
    clearing all button images and showing the standby image") it's a
    single firmware-level command that blanks every key and the touch
    strip in one call, and it's the same call `_safe_close` already uses
    for the disconnect-recovery path -- one reset semantics, not two.

    Called from `_run()`'s own outermost `finally`, which fires on every
    exit path from that function: a clean loop exit (`shutting_down` set by
    SIGTERM/SIGINT -- see `_install_signal_handler`), a normal return, or an
    uncaught exception propagating out of the loop. This is deliberately
    IN ADDITION TO (not instead of) `_run_active`'s own per-session
    `_safe_close` call: that one exists for disconnect/error recovery mid
    loop-iteration (so the outer hotplug loop can cleanly search for a
    replacement device) and fires on every session end, not just shutdown.
    This function is the single place that's *guaranteed* to run exactly
    once per `_run()` call, independent of which nested branch was active
    when shutdown was requested -- including the narrow windows where a
    device was found/opened but a session's own try/finally hadn't been
    entered yet. Calling `_safe_close` twice on the same already-closed
    device is harmless: `is_open()` is False by then, so the redundant
    `reset()` is skipped, and a redundant `close()` is swallowed by
    `_safe_close`'s own exception guard.

    Best-effort, like `_safe_close`: never raises, even if the device was
    unplugged, already closed, or claimed by another process by the time
    this runs -- a cleanup path that raises during shutdown would be worse
    than a stale screen. `deck` is None when shutdown happens before any
    device was ever found (or after the most recent one was unplugged),
    in which case there is nothing to blank and this is a no-op.

    Platform note: this only fires when the process gets a chance to run
    Python code at all. A hard kill -- SIGKILL, or Windows `TerminateProcess`
    (what `schtasks /End` uses against a Task Scheduler-launched process,
    since it isn't a console app `/End` can send WM_CLOSE to) -- bypasses
    the interpreter entirely; there is no hook point available for that
    case, and none is faked here.
    """
    if deck is None:
        return
    try:
        _safe_close(deck)
    except Exception:
        # Last-resort guard: _safe_close already catches its own internal
        # errors, but shutdown must never raise regardless of what future
        # changes might do to _safe_close.
        logger.exception("Unexpected error while blanking deck during shutdown")


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
        controls: Mapping[str, str] | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.deck = deck
        self.client = client
        self.hostname = hostname
        self.sort_mode = sort_mode
        # macOS app name of the local muxplex PWA to bring forward on a
        # key-press session switch; "" disables (see `.focus`).
        self.focus_app_name = focus_app_name
        # Read fresh on every wait in `_run_active`'s loop -- a plain
        # attribute (not captured into a closure), so `apply_reload` can
        # change it and the very next wait honors the new value.
        self.poll_interval = poll_interval

        # Guards every actual HTTP call -- poll-loop GETs, a dial-0 commit's
        # PATCH+refresh, and key-press connects can each originate from a
        # different thread.
        self.client_lock = threading.Lock()
        # Guards the live session/view/page state and the paint-diff caches.
        self.paint_lock = threading.Lock()

        # Capability-driven layout: FULL (Stream Deck+: dials + strip) keeps
        # the pre-existing behavior; REDUCED (Original/MK2/XL/Mini: no
        # dials/strip) reserves VIEW/PREV/NEXT keys instead -- both are now
        # a computed default table of (address -> action) bindings, with
        # `controls` (Gate-1-validated config overrides) merged on top. See
        # `.layout` and docs/CONTROL_MAPPING_DESIGN.md.
        self.plan = layout.plan_layout(layout.read_capabilities(deck), controls)

        self.view_cycler = ViewCycler()
        self.pager = Pager(page_size=max(1, self.plan.sessions_per_page))
        self.picker = PickerController()

        self.ordered: list[Session] = []
        self.active_session: str | None = None
        # Most recently displaced active session -- powers `toggle_last`.
        # Updated in lockstep with `active_session` via
        # `_note_active_session_locked`, both on local key-press connects
        # and on a server-side switch observed through `_process`.
        self.previous_session: str | None = None
        self.active_view: str = "all"
        self.session_names: list[str] = []  # current page's key-index -> session name

        # Session-local, never persisted: real hardware powers on dim
        # (`FULL_BRIGHTNESS_PERCENT` is asserted fresh on every bring-up in
        # `_run_active`), so writing a dimmed value to config.json would
        # fight that deliberate reset and could leave a deck that looks
        # dead after a replug with the cause stored invisibly in a file.
        # See docs/CONTROL_MAPPING_DESIGN.md §2.5.
        self.brightness: int = FULL_BRIGHTNESS_PERCENT

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

    def apply_reload(self, config: Config) -> None:
        """Apply a hot-reloaded config's safe fields to this live session.

        Called only when `config_mod.ConfigWatcher.poll()` reports that at
        least one of `config_mod.RELOADABLE_KEYS` actually changed --
        `server_url`/`key_file`/`ca_file` are never read from here (see
        that module's docstring on why: they're bound into the
        already-constructed `MuxplexClient`, not this object).

        Recomputes the Gate-2 plan against THIS deck's real capabilities --
        the same call bring-up (`_run_active`) makes -- so a control-mapping
        edit takes effect exactly like a fresh connection would compute it,
        with no reconnect needed. `sessions_per_page` may change if the
        edit moved which keys are reserved for view/page controls, so the
        pager's page size and page count are kept in lockstep; the paint
        cache is invalidated so the next `repaint()` redraws every key
        under the new bindings instead of trusting a diff cache computed
        under the old ones.
        """
        with self.paint_lock:
            self.plan = layout.plan_layout(
                layout.read_capabilities(self.deck), config.controls
            )
            self.sort_mode = config.sort
            self.focus_app_name = config.focus_app
            self.poll_interval = config.poll_interval
            self.pager.page_size = max(1, self.plan.sessions_per_page)
            self.pager.set_item_count(len(self.ordered))
            self.last_key_state = [None] * self.deck.key_count()
            self.last_strip = None
        _log_plan_diagnostics(self.plan)

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
            self._note_active_session_locked(server_state.active_session)
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
                # Unconditional in both modes: the default FULL-mode plan
                # has zero non-"session" key bindings, so this loop does
                # nothing there -- byte-identical to the old REDUCED-only
                # gate -- unless the user has remapped a FULL-mode key
                # away from "session".
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
        handled = {key_index for key_index, *_ in specs if key_index is not None}
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

        # Any OTHER remapped control key (e.g. a REDUCED-layout key bound to
        # "focus_app" instead of one of the three defaults) is inert while a
        # picker is open (§7's default-deny table) -- blank it so it never
        # shows stale normal-mode content. In the default config this set is
        # always empty (every non-session key IS one of the three specs
        # above), so this adds no extra paint calls for an unconfigured deck.
        for key_index in range(self.deck.key_count()):
            if key_index in handled or key_index in self.plan.session_slots:
                continue
            blank_identity: object = ("picker-blank",)
            if self.last_key_state[key_index] == blank_identity:
                continue
            self.deck.set_key_image(key_index, rendering.render_empty_key(self.deck))
            self.last_key_state[key_index] = blank_identity

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
        """Paint every key whose resolved action isn't "session", diffed.

        This is the generalized replacement for the old hardcoded
        VIEW/PREV/NEXT-only painter (docs/CONTROL_MAPPING_DESIGN.md calls
        this out as the riskiest edit in the whole change): it iterates
        every key index, skips the ones already painted as session tiles
        by `_paint_keys` (via `session_slots`), and for the rest either
        blanks a "none"-bound key or renders a labeled control key via
        `_control_key_display`. Unconditional in both layout modes -- see
        the call site in `_repaint_sessions`.
        """
        page = self.pager.page
        page_count = self.pager.page_count
        page_text = f"p{page}/{page_count}" if page_count > 1 else ""

        session_slot_set = set(self.plan.session_slots)
        for key_index in range(self.deck.key_count()):
            if key_index in session_slot_set:
                continue
            action = self.plan.bindings.get(f"key.{key_index}", "none")
            if action == "none":
                identity: object = ("control", "none")
                if self.last_key_state[key_index] == identity:
                    continue
                self.deck.set_key_image(
                    key_index, rendering.render_empty_key(self.deck)
                )
                self.last_key_state[key_index] = identity
                continue
            title, body, footer = _control_key_display(
                action,
                view_label=view_label,
                turning=turning,
                hostname=self.hostname,
                page_text=page_text,
            )
            identity = ("control", title, body, footer)
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

    def handle_dial_turn(self, dial: int, action: str, ticks: int) -> None:
        """Dispatch a dial turn by its resolved (relative-kind) action.

        `view_cycle`/`page_cycle` reuse the exact pre-existing behavior
        (normal-mode turn, or -- while their matching picker is open --
        scrolling that picker's window); `brightness_cycle` is new. Any
        other value (including "none", the default for an unassigned
        dial) is a no-op. `repaint()` is called unconditionally afterward,
        matching the pre-existing behavior of always refreshing the
        strip/keys even on an ignored turn.
        """
        label = f"dial[{dial}]"
        if action == "view_cycle":
            if self.picker.mode == PickerMode.VIEW:
                total = len(self.view_cycler.names())
                self.picker.scroll(ticks, total=total, page_size=self.deck.key_count())
            elif self.picker.mode == PickerMode.NONE:
                # Normal-mode turn behavior is unchanged -- only PRESS
                # changes meaning (see `PickerController`'s docstring).
                self.view_cycler.turn(ticks, self._commit_view)
            # else: PAGE picker is open -- this dial isn't its owner, ignore.
            self.repaint()
        elif action == "page_cycle":
            if self.picker.mode == PickerMode.PAGE:
                total = self.pager.page_count
                self.picker.scroll(ticks, total=total, page_size=self.deck.key_count())
            elif self.picker.mode == PickerMode.NONE:
                self.pager.turn(ticks)
            # else: VIEW picker is open -- this dial isn't its owner, ignore.
            self.repaint()
        elif action == "brightness_cycle":
            self._adjust_brightness(ticks * BRIGHTNESS_STEP_PERCENT, label)
        elif action == controls_mod.NONE_ACTION:
            logger.info("%s turn %+d (unassigned)", label, ticks)
        else:
            logger.info(
                "%s turn %+d -> action %r is not a dial-turn action (ignoring)",
                label,
                ticks,
                action,
            )

    def _commit_view(self, view: str) -> None:
        """Debounced (or press-immediate) view-cycle commit: PATCH, then refresh fast."""
        logger.info("view cycle commit -> %r", view)
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

    def handle_dial_push(self, dial: int, action: str) -> None:
        """Dispatch a dial push by its resolved (momentary-kind) action."""
        self._dispatch_control_action(action, slot=None, label=f"dial[{dial}]")

    # --- key handling ------------------------------------------------------

    def handle_key(self, key: int) -> None:
        """Dispatch a physical key press to its resolved action under the plan.

        Picker mode takes priority: in FULL mode (pickers open via dial
        presses) a tap always selects an option/page slot regardless of
        that key's normal-mode binding -- there's no reserved "BACK" key
        on an all-session-tile deck, so cancellation is always the
        owning dial's second push (see `PickerController`). In REDUCED
        mode (the VIEW key opens the view picker) taps dispatch through
        the pure `interaction.handle_picker_key`, which derives BACK/PAGE/
        SELECT/IGNORE from each pressed key's normal-mode action (§7).
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

        action, slot = layout.classify_key(self.plan, key)
        self._dispatch_control_action(action, slot=slot, label=f"key[{key}]")

    def _dispatch_control_action(
        self, action: str, *, slot: int | None, label: str
    ) -> None:
        """Shared dispatch for one momentary-action press, from a key or a dial push.

        `slot` is only meaningful for `"session"` (always None for a dial
        push, since a dial isn't a session tile -- see the module note
        below on binding `"session"`/`"none"` to a dial push).
        """
        if action == controls_mod.NONE_ACTION:
            logger.info("%s pressed (unassigned, ignoring)", label)
            return
        if action == controls_mod.SESSION_ACTION:
            if slot is None:
                logger.info(
                    "%s bound to 'session' but has no session slot -- ignoring",
                    label,
                )
                return
            self.connect_slot(slot, label)
            return
        if action == "view_picker":
            logger.info("%s -> %s", label, self.picker.press_view_dial())
            self.repaint()
        elif action == "page_picker":
            logger.info("%s -> %s", label, self.picker.press_page_dial())
            self.repaint()
        elif action == "page_prev":
            page = self.pager.turn(-1)
            logger.info("%s -> page %d", label, page)
            self.repaint()
        elif action == "page_next":
            page = self.pager.turn(1)
            logger.info("%s -> page %d", label, page)
            self.repaint()
        elif action == "view_all":
            logger.info("%s -> view ALL", label)
            threading.Thread(target=self._trigger_view_all, daemon=True).start()
        elif action == "page_first":
            page = self.pager.press()
            logger.info("%s -> page %d", label, page)
            self.repaint()
        elif action == "page_last":
            page = self.pager.go_to(self.pager.page_count)
            logger.info("%s -> page %d", label, page)
            self.repaint()
        elif action == "view_prev":
            logger.info("%s -> view prev", label)
            self.view_cycler.turn(-1, self._commit_view)
            self.repaint()
        elif action == "view_next":
            logger.info("%s -> view next", label)
            self.view_cycler.turn(1, self._commit_view)
            self.repaint()
        elif action == "focus_app":
            logger.info("%s -> focus", label)
            threading.Thread(
                target=focus.focus_app, args=(self.focus_app_name,), daemon=True
            ).start()
        elif action == "refresh_now":
            logger.info("%s -> refresh now", label)
            threading.Thread(target=self._refresh_now, daemon=True).start()
        elif action == "toggle_last":
            self._toggle_last(label)
        elif action == "brightness_up":
            self._adjust_brightness(BRIGHTNESS_STEP_PERCENT, label)
        elif action == "brightness_down":
            self._adjust_brightness(-BRIGHTNESS_STEP_PERCENT, label)
        else:
            logger.warning(
                "%s: action %r has no key/dial-push dispatch (relative-only "
                "action bound to a momentary address? Gate 1 should have "
                "rejected this)",
                label,
                action,
            )

    def _trigger_view_all(self) -> None:
        """Background-thread body for `view_all` -- jump to "all", no debounce.

        Revives `ViewCycler.press` (previously 0 callers -- see
        docs/CONTROL_MAPPING_DESIGN.md §1.3). Backgrounded like
        `_do_connect`/`focus_app`: `press()` calls `_commit_view`
        synchronously, which does a blocking PATCH+refresh -- never do
        that on the HID callback thread.
        """
        self.view_cycler.press(self._commit_view)
        self.repaint()

    def _refresh_now(self) -> None:
        """Background-thread body for `refresh_now`.

        No poll-loop surgery needed (§2.4): `refresh()` is already called
        from a non-poll thread elsewhere in this class (`_commit_view`),
        and serializes through `client_lock`/`paint_lock` exactly like a
        concurrent poll tick would.
        """
        try:
            self.refresh()
        except MuxplexError:
            logger.exception("refresh_now failed")

    def _toggle_last(self, label: str) -> None:
        """Connect the previously-active session, with a dead-session guard.

        `previous_session` is tracked by `_note_active_session_locked`
        every time `active_session` changes -- both from a local key-press
        connect and from a server-side switch observed via `_process`
        (someone switched sessions from the PWA).
        """
        target = self.previous_session
        if target is None:
            logger.info("%s TOGGLE pressed (no previous session)", label)
            return
        with self.paint_lock:
            exists = any(s.name == target for s in self.ordered)
        if not exists:
            logger.info(
                "%s TOGGLE pressed -> %r no longer exists, ignoring", label, target
            )
            return
        logger.info("%s TOGGLE pressed -> connect session %r", label, target)
        with self.paint_lock:
            self._note_active_session_locked(target)
        self.repaint()
        threading.Thread(target=self._do_connect, args=(target,), daemon=True).start()

    def _adjust_brightness(self, delta: int, label: str) -> None:
        """Step `self.brightness` by `delta`, clamped to [floor, 100], and apply it.

        Session-local only -- never written to config.json (see
        `self.brightness`'s field docstring).
        """
        self.brightness = max(
            BRIGHTNESS_FLOOR_PERCENT, min(100, self.brightness + delta)
        )
        logger.info("%s -> brightness %d%%", label, self.brightness)
        try:
            self.deck.set_brightness(self.brightness)
        except Exception:
            logger.exception("failed to set brightness to %d%%", self.brightness)

    def _note_active_session_locked(self, new_name: str | None) -> None:
        """Update `active_session` + `previous_session` together.

        Caller must hold `paint_lock`. The single home for this pairing so
        `toggle_last` sees every active-session change, whether it came
        from a local key-press connect (`connect_slot`) or a server-side
        switch observed by `_process` (someone switched in the PWA).
        """
        if new_name != self.active_session and self.active_session is not None:
            self.previous_session = self.active_session
        self.active_session = new_name

    def connect_slot(self, slot: int, label: str) -> None:
        """Connect the session shown in `slot` (pressed via `label`, e.g. "key[3]")."""
        with self.paint_lock:
            names = list(self.session_names)
        if slot >= len(names):
            logger.info("%s pressed (empty slot, ignoring)", label)
            return
        name = names[slot]
        logger.info(
            "%s pressed -> connect session %r (optimistic highlight)", label, name
        )
        # Move the highlight NOW (don't wait for the next poll tick) and run
        # the actual HTTP connect on a background thread -- real-hardware
        # feedback showed the old synchronous-on-the-callback-thread call
        # (server-side ttyd kill+respawn, ~2.6s) blocked further device
        # input and delayed the highlight by up to a full poll interval. The
        # PWA already does this optimistically; this mirrors it.
        with self.paint_lock:
            self._note_active_session_locked(name)
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
    """Dispatch a dial event by its RESOLVED ACTION, not a hardcoded dial index.

    Every dial index looks up its own `dial.N.turn`/`dial.N.push` binding
    from the plan -- there is nothing dial-0/dial-1-specific left here.
    Reclaiming the Deck+'s dials 2/3 (previously always logged as
    "unassigned") is exactly this generalization; see
    docs/CONTROL_MAPPING_DESIGN.md §4.4.
    """

    def on_dial(
        _deck: DeckDevice, dial: int, event_type: DialEventType, value: object
    ) -> None:
        if event_type == DialEventType.TURN:
            action = ctx.plan.bindings.get(
                f"dial.{dial}.turn", controls_mod.NONE_ACTION
            )
            ctx.handle_dial_turn(dial, action, int(value))  # type: ignore[arg-type]
        elif event_type == DialEventType.PUSH and value:
            action = ctx.plan.bindings.get(
                f"dial.{dial}.push", controls_mod.NONE_ACTION
            )
            ctx.handle_dial_push(dial, action)

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


def _unapplied_for_status(plan: layout.LayoutPlan) -> list[dict[str, str]] | None:
    """JSON-serializable form of `plan.unapplied`, or None when empty."""
    if not plan.unapplied:
        return None
    return [{"address": u.address, "reason": u.reason} for u in plan.unapplied]


def _log_plan_diagnostics(plan: layout.LayoutPlan) -> None:
    """Gate 2 diagnostics (§6): reported, never fatal -- a binding that

    doesn't apply to THIS deck may be perfectly valid for a different one
    the user swaps in later (hotplug), or may have just been introduced by
    a hot-reloaded edit. Surfaced at WARNING here (surface 1 of 4: bring-up
    log / reload log), in `status.json` (surface 2, via
    `_unapplied_for_status`), and via `doctor`/`muxplex-deck controls`
    (surfaces 3-4, in cli.py). Shared by bring-up (`_run_active`) and
    hot-reload (`_ActiveRuntime.apply_reload`) so both paths report
    identically.
    """
    for unapplied in plan.unapplied:
        logger.warning(
            "control binding %s does not apply to this deck: %s",
            unapplied.address,
            unapplied.reason,
        )
    for advisory in plan.advisories:
        logger.warning("control binding advisory: %s", advisory)


def _config_reload_status(
    mtime: float | None, outcome: config_mod.ReloadOutcome
) -> dict[str, Any]:
    """JSON-serializable `status.json` field for one *processed* reload check.

    Only called when `outcome.checked` is True -- `main._run_active`'s loop
    omits the `config_reload` key entirely on an unchanged-file tick, and
    `StatusReporter.update`'s merge-not-replace semantics mean the last
    processed outcome simply persists until the next one. `mtime` is
    config.json's mtime as of THIS check -- `cli.py`'s `controls set`/
    `unset`/`reset` compare against it to tell whether a specific edit has
    been picked up yet (see `cli._wait_for_config_pickup`).
    """
    return {
        "config_mtime": mtime,
        "checked_at": time.time(),
        "applied": list(outcome.applied),
        "restart_required": list(outcome.restart_required),
        "error": outcome.error,
    }


def _run_active(
    deck: DeckDevice,
    client: MuxplexClient,
    shutting_down: threading.Event,
    hostname: str,
    reporter: StatusReporter,
    watcher: config_mod.ConfigWatcher,
) -> None:
    """Run one connected-device session against the muxplex server.

    Returns when the device disconnects or shutdown is requested. Server
    errors (unreachable/auth) are handled *inside* this loop -- they never
    propagate up to trigger the outer hotplug recovery path, since they are
    expected, recoverable conditions, not device errors.

    `watcher` is polled once here (before anything else) so a *fresh*
    connection -- including a reconnect after the deck was unplugged for a
    while -- always starts from whatever config.json currently says, not a
    snapshot from an earlier bring-up; it's polled again every tick inside
    the loop below for genuine hot-reload while the connection stays up.
    """
    watcher.poll()
    config = watcher.current

    _log_device_info(deck)
    try:
        deck.set_brightness(FULL_BRIGHTNESS_PERCENT)
    except Exception:
        # Real hardware defaults to a dim power-on brightness; this always
        # asserts full brightness on bring-up (real-hardware feedback) --
        # but a device that just went away mid-open shouldn't be fatal here,
        # the outer loop's is_open()/connected() checks handle that.
        logger.exception("failed to set brightness to %d%%", FULL_BRIGHTNESS_PERCENT)
    ctx = _ActiveRuntime(
        deck,
        client,
        hostname,
        config.sort,
        config.focus_app,
        config.controls,
        config.poll_interval,
    )
    logger.info("%s", layout.describe_plan(ctx.plan))
    _log_plan_diagnostics(ctx.plan)
    _paint_status_only(deck, "connecting to muxplex...", ctx.plan)

    reporter.update(
        device_connected=True,
        device_caps=_describe_deck_caps(deck),
        unapplied=_unapplied_for_status(ctx.plan),
    )

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
        ctx.poll_interval,
        ctx.sort_mode,
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

            # Hot reload (§ config.py "Hot reload"): cheap on this existing
            # tick -- one `stat()` when nothing changed. Only a *processed*
            # check (`checked=True`) publishes `config_reload`; an
            # unchanged-file tick omits the key entirely and the reporter's
            # merge-not-replace semantics leave the last one in place.
            outcome = watcher.poll()
            if outcome.checked:
                mtime = watcher._stat_mtime()
                if outcome.error:
                    logger.error(
                        "config reload failed -- keeping last-known-good bindings: %s",
                        outcome.error,
                    )
                else:
                    assert outcome.config is not None
                    if outcome.applied:
                        ctx.apply_reload(outcome.config)
                        reporter.update(unapplied=_unapplied_for_status(ctx.plan))
                        logger.info(
                            "config reload applied: %s", ", ".join(outcome.applied)
                        )
                    if outcome.restart_required:
                        logger.warning(
                            "config change to %s requires a sidecar restart "
                            "to take effect",
                            ", ".join(outcome.restart_required),
                        )
                reporter.update(config_reload=_config_reload_status(mtime, outcome))

            if _interruptible_wait(deck, shutting_down, ctx.poll_interval):
                return
    finally:
        reporter.update(
            device_connected=False,
            device_caps=None,
            unapplied=None,
            server_connected=False,
        )
        _safe_close(deck)


class _FailureEpisode:
    """Tracks a repeating failure so it's logged (and expensively diagnosed)
    once per episode, not once per poll cycle.

    An "episode" is a run of consecutive failures sharing the same
    signature (`type(exc).__name__ + str(exc)`). The first failure in an
    episode logs at ERROR with `error_prefix` + a `build_detail()` result
    (computed ONLY here -- this is what keeps an expensive diagnosis, like
    shelling out to `usbipd.exe`, from running every 2 seconds forever);
    the full traceback goes to DEBUG. Subsequent identical failures are
    silent except for a periodic one-line heartbeat. `reset()` on success
    (or a changed signature, detected automatically) starts a fresh
    episode next time.
    """

    def __init__(self, heartbeat_seconds: float) -> None:
        self._heartbeat_seconds = heartbeat_seconds
        self._signature: str | None = None
        self._count = 0
        self._last_heartbeat = 0.0
        self.detail = ""

    def reset(self) -> None:
        self._signature = None
        self._count = 0
        self._last_heartbeat = 0.0
        self.detail = ""

    def note(
        self,
        exc: BaseException,
        *,
        build_detail: Callable[[], str],
        error_prefix: str,
        heartbeat_message: str,
    ) -> None:
        """Record one failure occurrence of `exc`."""
        signature = f"{type(exc).__name__}:{exc}"
        now = time.monotonic()
        if signature != self._signature:
            self._signature = signature
            self._count = 1
            self._last_heartbeat = now
            self.detail = build_detail()
            logger.error("%s%s", error_prefix, self.detail)
            logger.debug("failure detail", exc_info=exc)
            return
        self._count += 1
        if now - self._last_heartbeat >= self._heartbeat_seconds:
            logger.info(heartbeat_message, self._count)
            self._last_heartbeat = now


def _install_signal_handler() -> threading.Event:
    shutting_down = threading.Event()

    def handler(signum: int, frame: object) -> None:
        shutting_down.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    return shutting_down


def run(
    config: Config,
    manager: DeviceManager,
    *,
    log_file: Path | None = None,
    lock_path: Path | None = None,
    config_path: str | None = None,
) -> int:
    _configure_logging(log_file)

    # Single-instance guard -- see `.singleton`'s module docstring for why
    # this lives here rather than trusting any service manager's own
    # multiple-instance policy. Acquired BEFORE the signal handler and
    # BEFORE anything device/server-related, so a second instance exits
    # immediately and cleanly rather than racing the first for the
    # exclusive HID handle or the shared status file. Released in the
    # `finally` below, which -- because it wraps the entire loop -- fires
    # on every exit path: clean shutdown, an uncaught exception, or the
    # signal handler setting `shutting_down`.
    lock = InstanceLock(lock_path or default_lock_path())
    try:
        lock.acquire()
    except InstanceLockError as exc:
        logger.error(
            "%s -- another muxplex-deck instance is already running against "
            "this state directory. Exiting instead of racing it for the "
            "Stream Deck and the status file. Run `muxplex-deck status` to "
            "see the live instance; if you believe that's stale, stop it "
            "(`muxplex-deck service stop`, or Task Manager / kill the pid) "
            "before starting a new one.",
            exc,
        )
        return 1

    try:
        return _run(config, manager, config_path)
    finally:
        lock.release()


def _run(config: Config, manager: DeviceManager, config_path: str | None = None) -> int:
    """The hotplug + server-connectivity loop, guarded by `run()`'s lock."""
    shutting_down = _install_signal_handler()
    hostname = urlparse(config.server_url).hostname or config.server_url
    logged_waiting = False
    last_heartbeat = 0.0

    # Hot reload (see config.py's "Hot reload" section): one watcher for
    # the whole process lifetime, seeded with whatever `config` this
    # process started with. `_run_active` polls it on its existing
    # per-tick cadence -- see that function's docstring.
    watcher = config_mod.ConfigWatcher(config_path, config)

    # The most recently found device (or None, if absent/never found yet).
    # Tracked here -- not just inside `_run_active` -- so the `finally`
    # below can blank the screen on shutdown regardless of which nested
    # branch was executing when `shutting_down` was set: waiting for
    # hardware (None, nothing to blank), between `_find_deck` and
    # `deck.open()`, or mid `_run_active` session. See `_shutdown_cleanup`.
    current_deck: DeckDevice | None = None

    # Published for `muxplex-deck status` to read -- see `.statusfile`'s
    # docstring for why a running sidecar publishes its own status instead
    # of `status` probing the (possibly exclusively-held) device directly.
    reporter = StatusReporter(config.server_url)

    # Log-once-per-episode trackers -- see `_FailureEpisode`'s docstring.
    # Two separate instances: enumeration failures and open failures are
    # unrelated conditions and must not reset/confuse each other's episode.
    enumerate_episode = _FailureEpisode(ABSENT_HEARTBEAT_SECONDS)
    open_episode = _FailureEpisode(ABSENT_HEARTBEAT_SECONDS)

    logger.info("muxplex-deck starting (server=%s)", config.server_url)
    try:
        while not shutting_down.is_set():
            try:
                deck = _find_deck(manager)
                enumerate_episode.reset()
            except Exception as exc:  # noqa: BLE001 -- device backends raise varied errors; log + retry, never crash the loop
                enumerate_episode.note(
                    exc,
                    build_detail=lambda exc=exc: str(exc),
                    error_prefix="Unexpected error while enumerating Stream Deck devices; will retry: ",
                    heartbeat_message=(
                        "still failing to enumerate Stream Deck devices "
                        "(attempt %d, same error)"
                    ),
                )
                shutting_down.wait(DEVICE_POLL_SECONDS)
                continue

            current_deck = deck
            if deck is None:
                reporter.update(
                    device_connected=False,
                    device_caps=None,
                    server_connected=False,
                    hint=None,
                )
                open_episode.reset()
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
                open_episode.reset()
            except Exception as exc:  # noqa: BLE001 -- HID backends raise varied errors; report + retry, never crash the loop
                from . import hidhelp

                def _build_open_failure_detail(exc: BaseException = exc) -> str:
                    # Computed ONLY on a new episode -- may shell out to
                    # usbipd.exe on WSL, so it must never run per-cycle.
                    return hidhelp.explain_open_failure(str(exc)).message

                open_episode.note(
                    exc,
                    build_detail=_build_open_failure_detail,
                    error_prefix=f"cannot open the Stream Deck: {exc}\n",
                    heartbeat_message=(
                        "still cannot open the Stream Deck (attempt %d, same error)"
                    ),
                )
                # Fixes the stale-status/false-"Server: unreachable" bug: the
                # loop never got far enough to try the server, so without
                # this update the status file froze at whatever it last
                # said (see WSL_COLD_START_SPEC.md section 9.3 / V6).
                reporter.update(
                    device_connected=False,
                    device_caps=None,
                    server_connected=False,
                    hint=open_episode.detail,
                )
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
                        hostname,
                        reporter,
                        watcher,
                    )
            except Exception:
                logger.exception("Unexpected error during active session; recovering")
                reporter.update(
                    device_connected=False,
                    device_caps=None,
                    server_connected=False,
                    hint=None,
                )
                _safe_close(deck)
                shutting_down.wait(DEVICE_POLL_SECONDS)
    finally:
        logger.info("muxplex-deck shutting down")
        _shutdown_cleanup(current_deck)

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
