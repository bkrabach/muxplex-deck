"""Dial-driven interaction state: view cycling (dial 0) and paging (dial 1).

Pure state machines with no I/O -- `main.py` wires these to the device's
dial callback and to `MuxplexClient`/`rendering`. Keeping the *logic* here
(list position bookkeeping, debounce timing, page clamping) separate from
*painting* and *networking* keeps both testable and independently
regeneratable.

Thread-safety: dial turn/press events arrive on a device-callback thread --
the `streamdeck` library's own read thread for real hardware, or one of the
emulator's HTTP handler threads for `--emulator` -- while `main.py`'s poll
loop reads/refreshes this state from the main thread every `poll_interval`
seconds. Both classes guard their own state with an internal lock so callers
never need to coordinate access themselves.

`active_view` semantics (dial 0): muxplex's `active_view` is *global*
server-side state -- one value, last writer wins, shared by every
device/browser tab watching the server. `ViewCycler` mirrors that: turning
the dial changes what *everyone* sees, exactly like a browser tab switching
views would. There is no per-device view.

`PickerController` (dial-press picker mode): pressing dial 0 or dial 1 no
longer performs an immediate action (jump to "all" / reset to page 1) --
instead it hands the 8 keys over to a chooser listing that dial's options
(views, or page numbers), so a spin-then-tap sequence can jump straight to
a specific target on a server with more options than there are keys. This
class only tracks *which* picker (if any) owns the keys and its scroll
window -- it has no idea what the actual view names or page count are
(that's server-derived state `main.py` supplies at render/select time).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

DEFAULT_DEBOUNCE_SECONDS = 0.4


class ViewCycler:
    """Dial-0 state: local echo of the view being turned to + a debounced commit.

    A "turn" accumulates tick counts without touching the server. Only after
    `debounce_seconds` pass with no further ticks does the accumulated
    position get committed -- via the caller-supplied `on_commit` callback,
    invoked with the final view name -- so a fast multi-tick spin collapses
    into exactly one server write. A press jumps straight to "all",
    bypassing debounce entirely (an explicit, deliberate action).
    """

    def __init__(self, debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS) -> None:
        self._debounce_seconds = debounce_seconds
        self._lock = threading.Lock()
        self._names: list[str] = ["all"]
        self._committed_view = "all"
        self._pending_ticks = 0
        self._timer: threading.Timer | None = None

    def names(self) -> list[str]:
        """The current cycle list (`["all"] + <named views> + ["hidden"]`), a fresh copy."""
        with self._lock:
            return list(self._names)

    def sync(self, view_names: list[str], active_view: str) -> None:
        """Refresh the cycle list and the known server view from a fresh poll.

        `view_names` should be `settings.views` names in server order
        (before this method prepends "all" and appends "hidden").

        "hidden" is a reserved pseudo-view, exactly like "all": it is never
        a member of `settings.views` (the server's `views.RESERVED_VIEW_NAMES`
        rejects it as a user view name), but `active_view` already accepts it
        (GET/PATCH /api/state) and both the server's `filter_visible` and
        this sidecar's own `views.resolve_view` already treat it as a
        first-class case -- a session's hidden state is a property
        orthogonal to view membership, not a bucket alongside user views.
        The only gap was discoverability: this cycle list is the sidecar's
        sole source of selectable pseudo-view/view names, so appending
        "hidden" last (mirroring the PWA's own view dropdown, which
        hardcodes "All Sessions" first and "Hidden" as the always-last
        system view -- see `renderViewDropdown()` in muxplex's
        `frontend/app.js`) makes it reachable via dial-0 (FULL layout:
        normal spin-cycling and the dial-press picker) and the paged
        VIEW-key picker (REDUCED layout) -- both consume this same
        `names()` list, so this one change covers both hardware layouts.

        Only updates the tracked "committed" view when no turn is currently
        in flight (no pending debounce timer) -- otherwise a poll landing
        mid-spin would clobber the turn in progress out from under the
        user. Once the debounce fires (or a press happens), the next call
        to `sync` resumes tracking the server's value normally -- this is
        also what makes a *failed* PATCH self-heal: if `on_commit` raised,
        the optimistic `_committed_view` set at commit time gets corrected
        back to the real server value on the very next poll.
        """
        with self._lock:
            self._names = (
                ["all"]
                + [n for n in view_names if n not in ("all", "hidden")]
                + ["hidden"]
            )
            if self._timer is None:
                self._committed_view = active_view

    def _base_index(self) -> int:
        try:
            return self._names.index(self._committed_view)
        except ValueError:
            # Deleted/unknown current view: treat as sitting just before
            # "all" so the very next tick (either direction, since this is
            # a ring) lands somewhere sane rather than raising.
            return -1

    def _label_at(self, pending_ticks: int) -> str:
        if not self._names:
            return "all"
        index = (self._base_index() + pending_ticks) % len(self._names)
        return self._names[index]

    def is_turning(self) -> bool:
        """True while a turn's debounce timer is pending (not yet committed)."""
        with self._lock:
            return self._timer is not None

    def candidate_view(self) -> str:
        """The view name that would commit right now (mid-turn or settled)."""
        with self._lock:
            return self._label_at(self._pending_ticks)

    def turn(self, ticks: int, on_commit: Callable[[str], None]) -> str:
        """Register `ticks` of dial-0 rotation; returns the label to echo now.

        (Re)schedules `on_commit(label)` to fire after `debounce_seconds` of
        no further turns.
        """
        with self._lock:
            self._pending_ticks += ticks
            label = self._label_at(self._pending_ticks)
            if self._timer is not None:
                self._timer.cancel()
            timer = threading.Timer(
                self._debounce_seconds, self._fire_commit, args=(on_commit,)
            )
            timer.daemon = True
            self._timer = timer
            timer.start()
            return label

    def _fire_commit(self, on_commit: Callable[[str], None]) -> None:
        with self._lock:
            label = self._label_at(self._pending_ticks)
            self._committed_view = label
            self._pending_ticks = 0
            self._timer = None
        on_commit(label)

    def press(self, on_commit: Callable[[str], None]) -> str:
        """Dial-0 press: jump immediately to "all" (no debounce)."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            self._pending_ticks = 0
            self._committed_view = "all"
        on_commit("all")
        return "all"


class Pager:
    """Dial-1 state: local page position within the current view.

    Purely local -- turning or pressing dial 1 never talks to the server.
    `page` is 1-indexed. Turning clamps at the first/last page (no wrap,
    unlike the view cycler); pressing jumps back to page 1.
    """

    def __init__(self, page_size: int) -> None:
        self.page_size = max(1, page_size)
        self._lock = threading.Lock()
        self._page = 1
        self._page_count = 1

    def set_item_count(self, count: int) -> None:
        """Recompute page count from the current (view-resolved) item total.

        Clamps the current page down if the view shrank below it (e.g. a
        session closed, or switching to a smaller view without an explicit
        reset -- belt-and-suspenders alongside `main.py`'s explicit
        `reset()` on detected view changes).
        """
        with self._lock:
            self._page_count = max(1, math.ceil(count / self.page_size))
            self._page = min(self._page, self._page_count)

    def reset(self) -> None:
        """Back to page 1 -- called on every detected view change."""
        with self._lock:
            self._page = 1

    def turn(self, ticks: int) -> int:
        with self._lock:
            self._page = min(max(1, self._page + ticks), self._page_count)
            return self._page

    def press(self) -> int:
        with self._lock:
            self._page = 1
            return self._page

    def go_to(self, page: int) -> int:
        """Jump directly to `page` (1-indexed), clamped -- used by the page picker."""
        with self._lock:
            self._page = min(max(1, page), self._page_count)
            return self._page

    @property
    def page(self) -> int:
        with self._lock:
            return self._page

    @property
    def page_count(self) -> int:
        with self._lock:
            return self._page_count

    def slice_bounds(self) -> tuple[int, int]:
        """(start, stop) index range for the current page, for list slicing."""
        with self._lock:
            start = (self._page - 1) * self.page_size
            return start, start + self.page_size


class PickerMode(Enum):
    """Which dial-press picker (if any) currently owns the 8 keys."""

    NONE = "none"
    VIEW = "view"
    PAGE = "page"


class PickerController:
    """Dial-press picker state machine: NONE / VIEW / PAGE + a scroll window.

    Replaces the old "press dial 0 -> jump to all" / "press dial 1 -> reset
    to page 1" immediate actions with a chooser mode: pressing a dial hands
    the 8 keys over to a list of that dial's options (view names, or page
    numbers) so a spin-then-tap sequence can jump straight to a specific
    target -- useful once there are more views or pages than there are keys.

    Transition rules (all pure, no I/O):
    - Press the dial that owns the *current* picker -> exit to NONE.
    - Press the *other* dial while a picker is open -> switch straight to
      that dial's picker (no intermediate NONE).
    - Press either dial from NONE -> open that dial's picker.
    A session-key tap while a picker is open means "select this option", not
    "connect" -- that dispatch lives in `main.py` (it needs live server
    state this class deliberately doesn't carry).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mode = PickerMode.NONE
        self._window_start = 0

    @property
    def mode(self) -> PickerMode:
        with self._lock:
            return self._mode

    @property
    def window_start(self) -> int:
        with self._lock:
            return self._window_start

    def press_view_dial(self) -> PickerMode:
        """Dial-0 press: open the VIEW picker, or close it if already open."""
        with self._lock:
            self._mode = (
                PickerMode.NONE if self._mode == PickerMode.VIEW else PickerMode.VIEW
            )
            self._window_start = 0
            return self._mode

    def press_page_dial(self) -> PickerMode:
        """Dial-1 press: open the PAGE picker, or close it if already open."""
        with self._lock:
            self._mode = (
                PickerMode.NONE if self._mode == PickerMode.PAGE else PickerMode.PAGE
            )
            self._window_start = 0
            return self._mode

    def exit(self) -> None:
        """Close whichever picker is open (e.g. after a key-tap selection)."""
        with self._lock:
            self._mode = PickerMode.NONE
            self._window_start = 0

    def scroll(self, ticks: int, *, total: int, page_size: int) -> int:
        """Move the scroll window by `ticks` pages of `page_size` options.

        Clamped to `[0, last page start]`, no wrap -- matching `Pager.turn`'s
        clamping style. `ticks=0` is a no-op *move* but still re-clamps the
        stored window against a possibly-changed `total` (e.g. the view list
        or page count changed since the window was last set) -- callers use
        this to keep the window valid before every repaint.
        """
        with self._lock:
            self._window_start = clamp_window_start(
                self._window_start + ticks * page_size, total=total, page_size=page_size
            )
            return self._window_start

    def set_window(self, start: int) -> None:
        """Set the scroll window directly (already-clamped values only).

        Used by the reduced-layout key picker, whose paging math lives in
        the pure `handle_picker_key` -- the value it supplies came through
        `clamp_window_start`, and every repaint re-clamps via `scroll(0)`
        anyway, so a stale value can never paint out of range.
        """
        with self._lock:
            self._window_start = start


def clamp_window_start(start: int, *, total: int, page_size: int) -> int:
    """Clamp a picker scroll-window start to `[0, last page start]`.

    The one home for the picker's window math, shared by
    `PickerController.scroll` (dial-driven scrolling, FULL layout) and
    `handle_picker_key` (key-driven paging, REDUCED layout).
    """
    max_start = 0 if total <= page_size else ((total - 1) // page_size) * page_size
    return min(max(0, start), max_start)


# --- Reduced-layout view picker (key-driven) ---------------------------------
#
# On dial-less decks the VIEW key opens a paged view picker on the session
# grid itself: the session-slot keys show view names, the VIEW key becomes
# BACK, and PREV/NEXT page the list. The *dispatch* -- what a physical key
# press means while the picker is open -- is this pure function; `main.py`
# owns the side effects (PATCH active_view, repaint) and `PickerController`
# owns which picker is open.

ACTION_CANCEL = "cancel"  # BACK key: close the picker, change nothing
ACTION_SELECT = "select"  # a view was chosen: PATCH it (server-global), close
ACTION_PAGE = "page"  # PREV/NEXT: move the window, stay open
ACTION_IGNORE = "ignore"  # empty slot / unknown key: do nothing, stay open


@dataclass(frozen=True)
class PickerKeyResult:
    """Outcome of one key press while the reduced-layout picker is open.

    `view` is the chosen option's exact name (ACTION_SELECT only).
    `window_start` is the (clamped) scroll window after this press --
    unchanged except for ACTION_PAGE.
    """

    action: str
    view: str | None = None
    window_start: int = 0


def handle_picker_key(
    *,
    kind: str,
    slot: int | None,
    options: Sequence[str],
    window_start: int,
    page_size: int,
) -> PickerKeyResult:
    """Pure dispatch: map a classified key press to a picker action.

    Picker mode is *derived*, not separately configured (see
    docs/CONTROL_MAPPING_DESIGN.md §7): a key's normal-mode action
    determines its meaning while a picker is open, via a fixed,
    default-deny table --

        view_picker / page_picker  -> BACK   (ACTION_CANCEL)
        page_prev / page_next      -> page   (ACTION_PAGE)
        session                    -> option slot (ACTION_SELECT)
        anything else              -> ignored (ACTION_IGNORE)

    New catalog actions (`view_all`, `focus_app`, `brightness_*`, etc.)
    need no new rule here -- they simply fall through to ACTION_IGNORE,
    exactly like `none` always has.

    Args:
        kind: the pressed key's resolved action from `layout.classify_key`.
        slot: the session-slot position from `classify_key` (option index
            within the current window), or None when `kind != "session"`.
        options: the full option list (view names, in the same order the
            rest of the app uses -- `ViewCycler.names()`).
        window_start: the picker's current scroll-window start.
        page_size: options per page (the layout's `sessions_per_page`).

    Returns:
        A `PickerKeyResult`; never raises, even for empty `options`.
    """
    page_size = max(1, page_size)
    total = len(options)
    window_start = clamp_window_start(window_start, total=total, page_size=page_size)

    if kind in ("view_picker", "page_picker"):
        return PickerKeyResult(ACTION_CANCEL, window_start=window_start)
    if kind == "page_prev":
        new_start = clamp_window_start(
            window_start - page_size, total=total, page_size=page_size
        )
        return PickerKeyResult(ACTION_PAGE, window_start=new_start)
    if kind == "page_next":
        new_start = clamp_window_start(
            window_start + page_size, total=total, page_size=page_size
        )
        return PickerKeyResult(ACTION_PAGE, window_start=new_start)

    if kind != "session" or slot is None:
        return PickerKeyResult(ACTION_IGNORE, window_start=window_start)
    index = window_start + slot
    if index >= total:
        return PickerKeyResult(ACTION_IGNORE, window_start=window_start)
    return PickerKeyResult(
        ACTION_SELECT, view=options[index], window_start=window_start
    )
