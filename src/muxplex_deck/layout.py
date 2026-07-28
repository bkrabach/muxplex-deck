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
  grid shape alone (see `_reserved_control_keys`), never on model name.

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

--- User-configurable control mappings (docs/CONTROL_MAPPING_DESIGN.md) ---

`plan_layout(caps, overrides)` resolves every control (key, dial turn,
dial push) to a named *action* (see `.controls.ACTIONS`) via:

    resolved = default_bindings(caps) | overrides_applicable_to(caps)

Defaults are **computed, never stored** -- a user who configures nothing
sees behavior byte-identical to the pre-config-mapping implementation.
`overrides` is Gate 1 (`config.py`) already-validated address->action
pairs; this module performs Gate 2 (capability-aware applicability):
an override whose address index is out of range for *this* deck is
reported via `LayoutPlan.unapplied`, never fails the whole plan -- the
deck is hot-pluggable, and refusing to start would make the sidecar
unstartable whenever it's unplugged (see AGENTS.md's hotplug section).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import controls as controls_mod
from .device import DeckDevice

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
class Unapplied:
    """One Gate-2 diagnostic: a configured binding that cannot apply to this deck."""

    address: str
    reason: str


@dataclass(frozen=True)
class LayoutPlan:
    """The one decision object: which keys/controls this deck uses for what.

    `bindings` is the fully resolved (address -> action) map -- every
    control address this deck's capabilities describe, defaults merged
    with applicable overrides. `session_slots`/`sessions_per_page`/
    `use_dials` are derived from it (kept as separate fields since they're
    read on every repaint and are cheap to precompute once here).
    """

    mode: str  # MODE_FULL | MODE_REDUCED
    key_count: int
    bindings: Mapping[str, str]  # resolved address -> action, every control
    session_slots: tuple[int, ...]  # keys whose action == "session", ascending
    sessions_per_page: int
    use_dials: bool  # any dial.* address resolves to non-"none"?
    use_strip: bool  # paint the touch strip (status headline)? -- is_touch alone
    unapplied: tuple[Unapplied, ...]  # Gate-2 diagnostics (§6)
    advisories: tuple[str, ...]  # Gate-2 advisory warnings -- legal but self-defeating

    def _first_key_for_action(self, action: str) -> int | None:
        """First (ascending) key index bound to `action`, or None.

        Binding two keys to the same control action is legal -- both work
        at runtime -- but only the first gets special-cased painting
        treatment (e.g. the reduced-layout picker's BACK/PREV/NEXT chrome).
        """
        matches = sorted(
            int(address.split(".")[1])
            for address, bound_action in self.bindings.items()
            if address.startswith("key.") and bound_action == action
        )
        return matches[0] if matches else None

    @property
    def view_key(self) -> int | None:
        """First key bound to `view_picker`, or None if none is."""
        return self._first_key_for_action("view_picker")

    @property
    def prev_key(self) -> int | None:
        """First key bound to `page_prev`, or None if none is."""
        return self._first_key_for_action("page_prev")

    @property
    def next_key(self) -> int | None:
        """First key bound to `page_next`, or None if none is."""
        return self._first_key_for_action("page_next")


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


def default_bindings(caps: Mapping[str, Any]) -> dict[str, str]:
    """The capability-derived default (address -> action) table for `caps`.

    This is the pre-config-mapping behavior, expressed in the action
    catalog's vocabulary -- never written to disk (§4.2). Merging a user's
    (empty, by default) overrides on top of this must reproduce today's
    behavior exactly.
    """
    key_count = int(caps["key_count"])
    rows = int(caps["key_rows"])
    cols = int(caps["key_cols"])
    dial_count = int(caps["dial_count"])
    is_touch = bool(caps["is_touch"])

    bindings: dict[str, str] = {}

    if dial_count >= _FULL_MODE_MIN_DIALS and is_touch:
        for key in range(key_count):
            bindings[f"key.{key}"] = controls_mod.SESSION_ACTION
        if dial_count >= 1:
            bindings["dial.0.turn"] = "view_cycle"
            bindings["dial.0.push"] = "view_picker"
        if dial_count >= 2:
            bindings["dial.1.turn"] = "page_cycle"
            bindings["dial.1.push"] = "page_picker"
        for dial in range(2, dial_count):
            bindings[f"dial.{dial}.turn"] = controls_mod.NONE_ACTION
            bindings[f"dial.{dial}.push"] = controls_mod.NONE_ACTION
        return bindings

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
        for key in range(key_count):
            bindings[f"key.{key}"] = controls_mod.SESSION_ACTION
    else:
        bindings[f"key.{view_key}"] = "view_picker"
        bindings[f"key.{prev_key}"] = "page_prev"
        bindings[f"key.{next_key}"] = "page_next"
        for key in range(key_count):
            if key not in reserved:
                bindings[f"key.{key}"] = controls_mod.SESSION_ACTION

    # REDUCED mode never uses dials by default (matches today's
    # use_dials=False), but a deck that HAS dials still gets them listed as
    # explicit "none" bindings -- reclaimable via an override (§4.4).
    for dial in range(dial_count):
        bindings[f"dial.{dial}.turn"] = controls_mod.NONE_ACTION
        bindings[f"dial.{dial}.push"] = controls_mod.NONE_ACTION
    return bindings


def _split_overrides(
    overrides: Mapping[str, str], *, key_count: int, dial_count: int
) -> tuple[dict[str, str], tuple[Unapplied, ...]]:
    """Partition Gate-1-validated overrides into (applicable, unapplied) for this deck.

    Gate 2 (§6): an override is inapplicable when its address index is out
    of range for the deck's actual key/dial count. Never raises -- an
    inapplicable override is reported, not refused (see module docstring).
    """
    applicable: dict[str, str] = {}
    unapplied: list[Unapplied] = []
    for address_text, action in overrides.items():
        try:
            address = controls_mod.parse_address(address_text)
        except controls_mod.AddressError:
            # Gate 1 (config.py) already rejects malformed addresses before
            # this ever runs; this is just defense-in-depth against a
            # caller that skipped Gate 1 (e.g. a future direct API user).
            unapplied.append(Unapplied(address_text, "not a valid control address"))
            continue

        if address.control == "key":
            if address.index >= key_count:
                reason = (
                    f"this deck has {key_count} key"
                    + ("" if key_count == 1 else "s")
                    + (
                        f" (key.0 - key.{key_count - 1})"
                        if key_count > 1
                        else " (key.0)"
                    )
                )
                unapplied.append(Unapplied(address_text, reason))
                continue
        else:  # dial
            if dial_count == 0:
                unapplied.append(Unapplied(address_text, "this deck has no dials"))
                continue
            if address.index >= dial_count:
                reason = (
                    f"this deck has {dial_count} dial"
                    + ("" if dial_count == 1 else "s")
                    + (
                        f" (dial.0 - dial.{dial_count - 1})"
                        if dial_count > 1
                        else " (dial.0)"
                    )
                )
                unapplied.append(Unapplied(address_text, reason))
                continue

        applicable[address.text] = action
    return applicable, tuple(unapplied)


def _advisories(bindings: Mapping[str, str]) -> tuple[str, ...]:
    """Gate-2 advisory warnings (§6): legal but self-defeating configurations.

    Never blocks anything -- a user who only ever uses the `all` view is
    entitled to unbind `view_picker`, for example.
    """
    actions = set(bindings.values())
    warnings: list[str] = []
    if controls_mod.SESSION_ACTION not in actions:
        warnings.append(
            "no control is bound to 'session' -- this deck cannot connect any session"
        )
    if "view_picker" in actions and not (
        {"page_prev", "page_next", "page_cycle"} & actions
    ):
        warnings.append(
            "the view picker is bound but nothing pages it -- views past the "
            "first page will be unreachable"
        )
    if not ({"view_picker", "view_cycle", "view_all"} & actions):
        warnings.append(
            "no control changes the view -- the deck will stay on whatever "
            "view the server has"
        )
    return tuple(warnings)


def plan_layout(
    caps: Mapping[str, Any], overrides: Mapping[str, str] | None = None
) -> LayoutPlan:
    """Choose the layout mode and resolve every control's action for this deck.

    Args:
        caps: capability dict with `key_count`, `key_rows`, `key_cols`,
            `dial_count`, `is_touch` (see `read_capabilities`).
        overrides: Gate-1-validated (address -> action) pairs from config
            (`Config.controls`). Defaults to empty -- a fresh install's
            plan is purely capability-derived.

    Returns:
        A `LayoutPlan`; never raises for a weird-but-real deck shape or an
        override that doesn't apply to it (see `Unapplied`).
    """
    overrides = overrides or {}
    key_count = int(caps["key_count"])
    dial_count = int(caps["dial_count"])
    is_touch = bool(caps["is_touch"])

    mode = (
        MODE_FULL if dial_count >= _FULL_MODE_MIN_DIALS and is_touch else MODE_REDUCED
    )

    defaults = default_bindings(caps)
    applicable, unapplied = _split_overrides(
        overrides, key_count=key_count, dial_count=dial_count
    )
    bindings: dict[str, str] = {**defaults, **applicable}

    session_slots = tuple(
        sorted(
            int(address.split(".")[1])
            for address, action in bindings.items()
            if address.startswith("key.") and action == controls_mod.SESSION_ACTION
        )
    )
    use_dials = any(
        action != controls_mod.NONE_ACTION
        for address, action in bindings.items()
        if address.startswith("dial.")
    )

    return LayoutPlan(
        mode=mode,
        key_count=key_count,
        bindings=bindings,
        session_slots=session_slots,
        sessions_per_page=max(1, len(session_slots)),
        use_dials=use_dials,
        use_strip=is_touch,
        unapplied=unapplied,
        advisories=_advisories(bindings),
    )


def classify_key(plan: LayoutPlan, key: int) -> tuple[str, int | None]:
    """Map a physical key press to its resolved action under `plan`.

    Returns:
        `(action, slot)` where `action` is the catalog name bound to this
        key (see `.controls.ACTIONS`) and `slot` is the session-slot
        position (index into the current page's session list) when
        `action == "session"`, else None. A key outside the plan entirely
        (shouldn't happen on real hardware) classifies as
        `("session", None)` -- callers treat a `None` slot as an empty
        press and ignore it.
    """
    action = plan.bindings.get(f"key.{key}")
    if action is None:
        return (controls_mod.SESSION_ACTION, None)
    if action == controls_mod.SESSION_ACTION:
        try:
            return (controls_mod.SESSION_ACTION, plan.session_slots.index(key))
        except ValueError:
            return (controls_mod.SESSION_ACTION, None)
    return (action, None)


def describe_plan(plan: LayoutPlan) -> str:
    """One-line human-readable summary for the bring-up log."""
    if plan.mode == MODE_FULL:
        return (
            f"full layout: {plan.key_count} session keys/page, "
            "dial0=view cycling, dial1=paging, strip=status"
        )
    if plan.view_key is None:
        return (
            f"reduced layout: all {plan.key_count} keys are session tiles, "
            "no view/page controls bound"
        )
    return (
        f"reduced layout: {plan.sessions_per_page} session keys/page, "
        f"key[{plan.view_key}]=VIEW (tap opens picker), "
        + (
            f"key[{plan.prev_key}]=PREV, key[{plan.next_key}]=NEXT"
            if plan.prev_key is not None and plan.next_key is not None
            else "no PREV/NEXT bound"
        )
        + ("" if not plan.use_strip else ", strip=status")
    )
