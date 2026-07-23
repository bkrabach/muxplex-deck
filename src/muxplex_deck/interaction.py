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
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable

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

    def sync(self, view_names: list[str], active_view: str) -> None:
        """Refresh the cycle list and the known server view from a fresh poll.

        `view_names` should be `settings.views` names in server order
        (before the caller prepends "all"; "hidden" is excluded here too,
        matching the spec's cycle list of ["all"] + named views).

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
            self._names = ["all"] + [
                n for n in view_names if n not in ("all", "hidden")
            ]
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
            if self._page > self._page_count:
                self._page = self._page_count

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
