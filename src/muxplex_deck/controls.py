"""Control-mapping action catalog and capability-space address grammar.

Single source of truth for "what can be bound to what" -- shared by
`config.py` (Gate 1: capability-blind validation at load time),
`layout.py` (Gate 2: capability-aware applicability + default bindings),
and `cli.py` (the `controls` subcommand group and its help text). See
`docs/CONTROL_MAPPING_DESIGN.md` for the full design.

Addresses are coordinates in capability space -- `key.N`, `dial.N.turn`,
`dial.N.push` -- never model names or layout-mode names (AGENTS.md's
"capability-driven, never model-name-driven" rule, applied to config).

Each action has a **kind**:

- ``MOMENTARY`` -- fires on a discrete press. Valid on ``key.N`` and
  ``dial.N.push``.
- ``RELATIVE`` -- consumes a signed tick count. Valid on ``dial.N.turn``
  only.

**Judgment call:** ``none`` (kind ``MOMENTARY`` in the catalog below) is
treated as valid on *every* address, including ``dial.N.turn`` --
"unassign this control" is a universally meaningful operation regardless
of what kind of signal the control produces, and the computed defaults
for unused dials (`layout.default_bindings`) rely on being able to write
``dial.N.turn -> "none"``. The design's own kind table doesn't explicitly
carve out this exception; see `valid_actions_for_address`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MOMENTARY = "momentary"
RELATIVE = "relative"

NONE_ACTION = "none"
SESSION_ACTION = "session"


@dataclass(frozen=True)
class ActionSpec:
    """One catalog entry: its kind, and a one-line description for `controls actions`."""

    kind: str
    help: str


# The full 19-action catalog (Tier 1 + Tier 2 of the design).
ACTIONS: dict[str, ActionSpec] = {
    # --- Tier 1: pure remaps of existing behavior ---
    SESSION_ACTION: ActionSpec(
        MOMENTARY,
        "Connect the session shown in this slot (default for any key with no other binding)",
    ),
    "view_picker": ActionSpec(MOMENTARY, "Open/close the paged view picker"),
    "page_picker": ActionSpec(MOMENTARY, "Open/close the page picker"),
    "page_prev": ActionSpec(MOMENTARY, "Page -1 (clamped)"),
    "page_next": ActionSpec(MOMENTARY, "Page +1 (clamped)"),
    NONE_ACTION: ActionSpec(
        MOMENTARY, "Unassigned -- key paints blank, press logs and is ignored"
    ),
    "view_cycle": ActionSpec(RELATIVE, "Debounced active-view change by dial ticks"),
    "page_cycle": ActionSpec(RELATIVE, "Local paging by dial ticks (no server write)"),
    # --- Tier 2: reachable machinery that nothing currently calls ---
    "view_all": ActionSpec(MOMENTARY, "Jump straight to the 'all' view, no debounce"),
    "page_first": ActionSpec(MOMENTARY, "Jump straight to page 1"),
    "page_last": ActionSpec(MOMENTARY, "Jump to the final page"),
    "view_prev": ActionSpec(MOMENTARY, "Step one view back"),
    "view_next": ActionSpec(MOMENTARY, "Step one view forward"),
    "focus_app": ActionSpec(MOMENTARY, "Bring the muxplex PWA to the foreground"),
    "refresh_now": ActionSpec(MOMENTARY, "Poll the server immediately"),
    "toggle_last": ActionSpec(MOMENTARY, "Connect the previously-active session"),
    "brightness_up": ActionSpec(MOMENTARY, "Brightness +10% (floor 10%, cap 100%)"),
    "brightness_down": ActionSpec(MOMENTARY, "Brightness -10% (floor 10%, cap 100%)"),
    "brightness_cycle": ActionSpec(
        RELATIVE, "Brightness by dial ticks (floor 10%, cap 100%)"
    ),
}


@dataclass(frozen=True)
class Address:
    """A parsed control address: `key.N` or `dial.N.turn`/`dial.N.push`."""

    control: str  # "key" | "dial"
    index: int
    sub: str | None  # "turn" | "push" for dial; None for key

    @property
    def text(self) -> str:
        """Canonical string form -- always re-derived, never trusted from input."""
        if self.control == "key":
            return f"key.{self.index}"
        return f"dial.{self.index}.{self.sub}"

    @property
    def is_relative_only(self) -> bool:
        """True for `dial.N.turn` -- the one address form that takes tick counts."""
        return self.control == "dial" and self.sub == "turn"


class AddressError(ValueError):
    """Raised when a control address string doesn't match the grammar (§3)."""


# key.N (no leading zeros, no sign) | dial.N.turn | dial.N.push
_ADDRESS_RE = re.compile(r"^key\.(0|[1-9][0-9]*)$|^dial\.(0|[1-9][0-9]*)\.(turn|push)$")


def parse_address(text: str) -> Address:
    """Parse a control address string. Raises `AddressError` on any grammar violation."""
    if not isinstance(text, str):
        raise AddressError(f"invalid control address {text!r} (must be a string)")
    match = _ADDRESS_RE.match(text)
    if not match:
        raise AddressError(
            f"invalid control address {text!r}. Valid forms: key.N, dial.N.turn, dial.N.push"
        )
    if match.group(1) is not None:
        return Address("key", int(match.group(1)), None)
    return Address("dial", int(match.group(2)), match.group(3))


def valid_actions_for_address(address: Address) -> tuple[str, ...]:
    """Actions legal for this address, sorted -- the set Gate 1's kind check enforces.

    `none` is always included regardless of kind -- see the module docstring's
    judgment-call note.
    """
    wanted_kind = RELATIVE if address.is_relative_only else MOMENTARY
    names = {name for name, spec in ACTIONS.items() if spec.kind == wanted_kind}
    names.add(NONE_ACTION)
    return tuple(sorted(names))


def validate_binding(address_text: str, action: str) -> Address:
    """Parse + fully validate one (address, action) pair.

    Raises `AddressError` for a malformed address, or `ValueError` for an
    unknown action or a kind mismatch. Used by both Gate 1 (`config.py`,
    capability-blind) and the `controls set` CLI subcommand (which must
    reject exactly what Gate 1 would reject, since a value it accepts is
    about to be written to the same config file Gate 1 validates).
    """
    address = parse_address(address_text)
    if action not in ACTIONS:
        raise ValueError(
            f"unknown action {action!r} for {address_text!r}. Valid actions: "
            f"{', '.join(sorted(ACTIONS))}"
        )
    valid_for_address = valid_actions_for_address(address)
    if action not in valid_for_address:
        target = "a dial turn" if address.is_relative_only else "a key/dial push"
        raise ValueError(
            f"action {action!r} cannot be bound to {address_text!r} -- {target} "
            f"accepts only: {', '.join(valid_for_address)}"
        )
    return address


def catalog_help_lines() -> list[str]:
    """One line per catalog action: name, kind, description -- for `controls actions`."""
    lines = []
    for name in sorted(ACTIONS):
        spec = ACTIONS[name]
        lines.append(f"  {name} ({spec.kind}): {spec.help}")
    return lines
