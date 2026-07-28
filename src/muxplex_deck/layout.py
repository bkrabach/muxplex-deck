"""Capability-driven key layout planning: which keys do what on this deck.

Mirrors `deck_probe/capabilities.py`'s approach (pure dict in, decisions
out) without importing it -- the probe is a PoC, this is the product. The
hard rule is the same: **branch only on numeric/boolean capability values
(`key_count`, `key_layout`, `dial_count`, `is_touch`), never on
`deck_type()` strings** -- model names collide (Original vs MK2) and new
models would need matrix updates. Capabilities self-describe.

Two layout modes:

- FULL -- decks with at least two dials AND a touchscreen (the Stream
  Deck+). Every LCD key is a session tile; dial 0 cycles views, dial 1
  pages, and the touch strip carries the server/view/status headline.
  This is the pre-existing Stream Deck+ behavior, unchanged.
- REDUCED -- everything else (Original/MK2 3x5, XL 4x8, Mini 3x2, ...).
  With no dials or strip to lean on, three keys are reserved *by grid
  position* (computed from `key_layout` rows x cols, never hardcoded)
  for the roles the dials/strip played. Which three positions depends on
  grid shape alone (see `_reserved_control_keys`), never on model name:

    * Exactly 3 columns, 2+ rows (e.g. the Stream Deck Mini, 3 cols x 2
      rows): 3 columns is exactly enough to dedicate the *entire bottom
      row* to controls, reading left to right as PREV, VIEW, NEXT --
      every row above it is entirely session tiles. This was chosen over
      reusing the corner layout below because on a 3-wide grid the
      corner scheme leaves session tiles awkwardly split around a lone
      reserved key in the bottom row (e.g. keys 1, 2, 4 on a 3x2 grid --
      a gap where key 3 (PREV) sits), whereas a full control row leaves
      every session tile contiguous on the row(s) above.
    * Everything else (any other column count): reserve the three
      corners -- VIEW top-left (index 0), PREV bottom-left
      (index (rows-1)*cols), NEXT bottom-right (index rows*cols-1) --
      the original REDUCED geometry, ported from dial-0/dial-1's roles
      on the Stream Deck+. Unchanged for the 15-key Original (3x5) and
      XL (4x8).

  In both cases VIEW shows the current view name + server label (what
  the strip showed); a tap opens a paged view picker on the session-slot
  keys (dial-0's picker role) -- VIEW becomes BACK, PREV/NEXT page the
  list, tapping a view selects it. Every remaining key is a session tile
  in reading order, so sessions_per_page = key_count - 3 (12 on a 15-key
  deck, 3 on a 6-key Mini).

Edge cases (degrade gracefully, never crash):

- Dials but no touchscreen: REDUCED. The key controls always work; the
  dials are left unassigned rather than inventing a third half-mode.
- Touchscreen but fewer than two dials: REDUCED for the keys, but the
  strip is still used for the status headline (`use_strip` stays True) --
  free extra signal, no behavior depends on it.
- Fewer than 4 keys, or a grid so degenerate the three reserved control
  keys collide (e.g. a single row, where top-left == bottom-left): every
  key becomes a session tile with no view/page controls -- a plain
  session switcher beats three controls with no sessions.

Everything here is pure (no device I/O, no threads), so both layout modes
are unit-testable with fake capability dicts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .device import DeckDevice

# classify_key result kinds
KEY_VIEW = "view"
KEY_PREV = "prev"
KEY_NEXT = "next"
KEY_SESSION = "session"

MODE_FULL = "full"
MODE_REDUCED = "reduced"

# FULL mode needs one dial for view cycling and one for paging.
_FULL_MODE_MIN_DIALS = 2
# REDUCED mode reserves 3 control keys and needs at least 1 session slot.
_REDUCED_MIN_KEYS = 4


def read_capabilities(deck: DeckDevice) -> dict[str, Any]:
    """Build the capability dict `plan_layout` consumes, from a live device.

    Pure with respect to the deck: only calls capability accessors (class
    constants on every `streamdeck` model), no device I/O.
    """
    rows, cols = deck.key_layout()
    return {
        "key_count": deck.key_count(),
        "key_rows": rows,
        "key_cols": cols,
        "dial_count": deck.dial_count(),
        "is_touch": deck.is_touch(),
    }


@dataclass(frozen=True)
class LayoutPlan:
    """The one decision object: which keys/controls this deck uses for what.

    `session_slots` maps *slot position* (index into the current page's
    session list, reading order) -> *physical key index*. In FULL mode it
    is simply `range(key_count)`; in REDUCED mode it skips the reserved
    control keys.
    """

    mode: str  # MODE_FULL | MODE_REDUCED
    key_count: int
    view_key: int | None  # reserved control keys (REDUCED mode only)
    prev_key: int | None
    next_key: int | None
    session_slots: tuple[int, ...]
    sessions_per_page: int
    use_dials: bool  # attach dial callbacks (view/page cycling)?
    use_strip: bool  # paint the touch strip (status headline)?


def _reserved_control_keys(rows: int, cols: int) -> tuple[int, int, int]:
    """Compute `(view_key, prev_key, next_key)` physical key indices.

    Chosen by grid shape alone (`key_rows` x `key_cols`) -- never by model
    name. Two geometries:

    - Exactly 3 columns, 2+ rows: the bottom row exactly fits all three
      controls, reading left to right as PREV, VIEW, NEXT. Every row above
      it is entirely session tiles. A 3-wide grid is the one shape where a
      full control row is both possible (no leftover) and better than the
      corner scheme below, which would otherwise strand session tiles
      around a lone reserved key mid-row.
    - Everything else: reserve the three corners -- VIEW top-left, PREV
      bottom-left, NEXT bottom-right -- the original REDUCED geometry.
    """
    if cols == 3 and rows >= 2:
        bottom_row_start = (rows - 1) * cols
        return bottom_row_start + 1, bottom_row_start, bottom_row_start + 2
    return 0, (rows - 1) * cols, rows * cols - 1


def plan_layout(caps: Mapping[str, Any]) -> LayoutPlan:
    """Choose the layout mode and key assignments for a deck's capabilities.

    Args:
        caps: capability dict with `key_count`, `key_rows`, `key_cols`,
            `dial_count`, `is_touch` (see `read_capabilities`).

    Returns:
        A `LayoutPlan`; never raises for a weird-but-real deck shape.
    """
    key_count = int(caps["key_count"])
    rows = int(caps["key_rows"])
    cols = int(caps["key_cols"])
    dial_count = int(caps["dial_count"])
    is_touch = bool(caps["is_touch"])

    if dial_count >= _FULL_MODE_MIN_DIALS and is_touch:
        return LayoutPlan(
            mode=MODE_FULL,
            key_count=key_count,
            view_key=None,
            prev_key=None,
            next_key=None,
            session_slots=tuple(range(key_count)),
            sessions_per_page=key_count,
            use_dials=True,
            use_strip=True,
        )

    view_key, prev_key, next_key = _reserved_control_keys(rows, cols)
    reserved = (view_key, prev_key, next_key)
    degenerate = (
        key_count < _REDUCED_MIN_KEYS
        or len(set(reserved)) < 3
        or any(index >= key_count for index in reserved)
    )
    if degenerate:
        # Too few keys (or a grid where the corners collide) to spend three
        # on controls: every key is a session tile, no view/page controls.
        return LayoutPlan(
            mode=MODE_REDUCED,
            key_count=key_count,
            view_key=None,
            prev_key=None,
            next_key=None,
            session_slots=tuple(range(key_count)),
            sessions_per_page=max(1, key_count),
            use_dials=False,
            use_strip=is_touch,
        )

    session_slots = tuple(i for i in range(key_count) if i not in reserved)
    return LayoutPlan(
        mode=MODE_REDUCED,
        key_count=key_count,
        view_key=view_key,
        prev_key=prev_key,
        next_key=next_key,
        session_slots=session_slots,
        sessions_per_page=len(session_slots),
        use_dials=False,
        use_strip=is_touch,
    )


def classify_key(plan: LayoutPlan, key: int) -> tuple[str, int | None]:
    """Map a physical key press to its role under `plan`.

    Returns:
        `(KEY_VIEW | KEY_PREV | KEY_NEXT, None)` for a reserved control
        key, or `(KEY_SESSION, slot_position)` where `slot_position`
        indexes into the current page's session list. A key outside the
        plan entirely (shouldn't happen on real hardware) classifies as
        `(KEY_SESSION, None)` -- callers treat a `None` slot as an empty
        press and ignore it.
    """
    if key == plan.view_key:
        return (KEY_VIEW, None)
    if key == plan.prev_key:
        return (KEY_PREV, None)
    if key == plan.next_key:
        return (KEY_NEXT, None)
    try:
        return (KEY_SESSION, plan.session_slots.index(key))
    except ValueError:
        return (KEY_SESSION, None)


def describe_plan(plan: LayoutPlan) -> str:
    """One-line human-readable summary for the bring-up log."""
    if plan.mode == MODE_FULL:
        return (
            f"full layout: {plan.key_count} session keys/page, "
            "dial0=view cycling, dial1=paging, strip=status"
        )
    if plan.view_key is None:
        return (
            f"reduced layout (degenerate grid): all {plan.key_count} keys are "
            "session tiles, no view/page controls"
        )
    return (
        f"reduced layout: {plan.sessions_per_page} session keys/page, "
        f"key[{plan.view_key}]=VIEW (tap opens picker), key[{plan.prev_key}]=PREV, "
        f"key[{plan.next_key}]=NEXT" + ("" if not plan.use_strip else ", strip=status")
    )
