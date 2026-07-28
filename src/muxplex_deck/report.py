"""Terminal-output rendering for `doctor` / `status` / `service` -- pure text.

Three bands, always this order, separated by exactly ONE blank line, with
leading/trailing blank lines as the only frame::

    VERDICT     one line, column 0
    STATE       the checks or readouts, indented
    ACTION      "Do this:" + exactly one decision (omitted when nothing to do)

No ANSI color, no box drawing, no tree connectors -- the acid test is the
paste: the user's primary act is selecting this output and pasting it as
plain text, so every emphasis device must survive that. Output is
byte-identical whether printed to a TTY or piped to a file; the only thing
that varies with environment is the `+` (fine) glyph, which becomes a
Unicode checkmark when the environment is UTF-8-capable (see
`utf8_capable`). This module does no I/O itself -- callers decide what to
check and how to phrase it; this module only lays it out.

Column ladder (exact, see the layout-architect review this implements)::

    check line     2sp + glyph + 2sp + subject padded to 14 + value (col 19)
    continuation    19sp + text (hangs under the value column)
    readout line     5sp + name padded to 14 + value -- gutter left empty
    group label     column 0, lowercase, blank line above (--all only)
    command to run  indented +4 from its governing text
    prose in ACTION column 2, max 2 lines

Only VERDICT, "Do this:", and `--all` group labels ever touch column 0.
Below 60 columns, subject padding drops to a single space (no other
reflow); above 80, nothing changes -- fields are never stretched to fill
a wide terminal, and wrapping always happens at column 78.

The gutter law: the gutter is empty if and only if there is nothing to do.
A fully healthy report has no glyphs at all -- see `Readout` -- and `+`
appears only for contrast alongside a failure in the same report, or under
`--all`.
"""

from __future__ import annotations

import os
import re
import textwrap
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Glyphs -- exactly three, ASCII by design (see module docstring on UTF-8).
# ---------------------------------------------------------------------------

ACT = "!"  # act now -- the root cause; a command exists
FINE = "+"  # nothing to do
BLOCKED = "-"  # cannot be done or evaluated until an upstream item resolves

# Rollup severity for grouped checks: "!" > "-" > "+" (higher = worse).
_SEVERITY = {ACT: 2, BLOCKED: 1, FINE: 0}

# ---------------------------------------------------------------------------
# Column geometry
# ---------------------------------------------------------------------------

_GUTTER_WIDTH = 5  # "  " + glyph/space + "  "
_SUBJECT_WIDTH = 14
_NARROW_SUBJECT_WIDTH = 1  # below _NARROW_THRESHOLD, drop padding to 1 space
_NARROW_THRESHOLD = 60
_DEFAULT_WIDTH = 78
_ACTION_PROSE_COLUMN = 2
_ACTION_COMMAND_INDENT = 4  # "+4 from its governing text"


def _subject_field(subject: str, width: int) -> str:
    """The subject column's text, including its trailing separator.

    At normal widths this is `subject` left-justified to 14 columns. Below
    `_NARROW_THRESHOLD` columns, padding drops to a single separating space
    instead (the spec's "drop subject padding to a single space") -- the
    column ladder is no longer aligned at that width, which is expected.
    """
    if width < _NARROW_THRESHOLD:
        return f"{subject} "
    return subject.ljust(_SUBJECT_WIDTH)


# ---------------------------------------------------------------------------
# UTF-8 glyph substitution -- "+"/"✓" only. "!" and "-" never change: a
# diagnostic tool must not emit output that can be corrupted by the very
# environment it is diagnosing, so the ASCII forms are always valid; the
# checkmark is a cosmetic upgrade applied only when it is safe.
# ---------------------------------------------------------------------------


def utf8_capable(env: dict[str, str] | None = None) -> bool:
    """Best-effort: can this environment render a UTF-8 checkmark safely?

    Checks LC_ALL, LC_CTYPE, LANG in that order (the standard POSIX locale
    precedence) for a "UTF-8"/"utf8" marker. Defaults to `os.environ`;
    callers (and tests) may pass an explicit mapping instead.
    """
    source = os.environ if env is None else env
    for key in ("LC_ALL", "LC_CTYPE", "LANG"):
        value = source.get(key)
        if not value:
            continue
        normalized = value.lower()
        if "utf-8" in normalized or "utf8" in normalized:
            return True
    return False


def _glyph_char(glyph: str, *, utf8: bool) -> str:
    if glyph == FINE and utf8:
        return "\u2713"
    return glyph


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Check:
    """One line of STATE: a glyph, a subject, and a value."""

    subject: str
    glyph: str
    value: str


@dataclass
class Group:
    """A named cluster of `Check`s that collapses to one line by default.

    Default mode renders `rollup()` (glyph = worst member, value = that
    member's value). `--all` renders every member instead, under a
    column-0 group label -- a strict superset, never a different answer.
    """

    subject: str
    members: list[Check]

    def rollup(self) -> Check:
        worst = max(self.members, key=lambda c: _SEVERITY[c.glyph])
        return Check(subject=self.subject, glyph=worst.glyph, value=worst.value)


@dataclass
class Readout:
    """One line of STATE with no glyph at all -- the all-fine gutter-empty form."""

    name: str
    value: str


Item = Check | Group

# ---------------------------------------------------------------------------
# Wrapping -- hard-wrap at column 78 (or the given width), never soft-wrap.
# Lines that already fit are left byte-for-byte alone (so hand-formatted
# multi-line guidance blocks with their own bullet indentation survive);
# only a line that genuinely overflows gets reflowed, and only that line.
# ---------------------------------------------------------------------------


def _wrap_logical_line(line: str, avail: int) -> list[str]:
    avail = max(10, avail)
    if len(line) <= avail:
        return [line]
    wrapped = textwrap.wrap(
        line, width=avail, break_long_words=False, break_on_hyphens=False
    )
    return wrapped or [""]


def _render_value_block(
    prefix: str, value: str, *, value_column: int, width: int
) -> str:
    """Render `prefix` + `value`, wrapping/hanging continuations at `value_column`."""
    avail = max(10, width - value_column)
    logical_lines = value.splitlines() or [""]
    out: list[str] = []
    first = True
    for logical in logical_lines:
        for fragment in _wrap_logical_line(logical, avail):
            if first:
                out.append(prefix + fragment)
                first = False
            else:
                out.append(" " * value_column + fragment)
    return "\n".join(out)


def format_check_line(
    glyph: str, subject: str, value: str, *, utf8: bool, width: int = _DEFAULT_WIDTH
) -> str:
    """Render one STATE check line: `  <glyph>  <subject><value>` (+ wraps)."""
    display_glyph = _glyph_char(glyph, utf8=utf8)
    prefix = f"  {display_glyph}  {_subject_field(subject, width)}"
    return _render_value_block(prefix, value, value_column=len(prefix), width=width)


def format_readout_line(name: str, value: str, *, width: int = _DEFAULT_WIDTH) -> str:
    """Render one STATE readout line: gutter left empty (the all-fine form)."""
    prefix = " " * _GUTTER_WIDTH + _subject_field(name, width)
    return _render_value_block(prefix, value, value_column=len(prefix), width=width)


def group_label(subject: str) -> str:
    """The `--all`-only column-0 group heading (lowercase, blank line above it)."""
    return subject.lower()


# ---------------------------------------------------------------------------
# Rendering a list of Check/Group items into STATE lines
# ---------------------------------------------------------------------------


def collapsed_checks(items: list[Item]) -> list[Check]:
    """Flatten groups to their rollup -- the representation the VERDICT/ACTION

    bands are always computed from, so both bands stay identical between
    default and `--all` mode (the superset property: only the middle
    expands).
    """
    return [item.rollup() if isinstance(item, Group) else item for item in items]


def render_items(
    items: list[Item], *, show_all: bool, utf8: bool, width: int = _DEFAULT_WIDTH
) -> list[str]:
    """Render STATE lines for a list of Check/Group items.

    Default mode: each Group renders as its single rolled-up line. `--all`:
    each Group expands into a column-0 lowercase label followed by every
    member as its own check line; ungrouped Checks render identically in
    both modes (nothing to expand).
    """
    lines: list[str] = []
    need_blank_before_next_group = False
    for item in items:
        if isinstance(item, Group):
            if show_all:
                if need_blank_before_next_group:
                    lines.append("")
                lines.append(group_label(item.subject))
                for member in item.members:
                    lines.append(
                        format_check_line(
                            member.glyph,
                            member.subject,
                            member.value,
                            utf8=utf8,
                            width=width,
                        )
                    )
                need_blank_before_next_group = True
                continue
            rolled = item.rollup()
            lines.append(
                format_check_line(
                    rolled.glyph, rolled.subject, rolled.value, utf8=utf8, width=width
                )
            )
        else:
            lines.append(
                format_check_line(
                    item.glyph, item.subject, item.value, utf8=utf8, width=width
                )
            )
        need_blank_before_next_group = False
    return lines


def render_readouts(
    readouts: list[Readout], *, width: int = _DEFAULT_WIDTH
) -> list[str]:
    return [format_readout_line(r.name, r.value, width=width) for r in readouts]


# ---------------------------------------------------------------------------
# VERDICT -- counts ACTIONS, not problems.
# ---------------------------------------------------------------------------


def count_actions(glyphs: list[str]) -> int:
    return sum(1 for g in glyphs if g == ACT)


def verdict_readiness(action_count: int, *, note: str | None = None) -> str:
    """ "Ready."/"Not ready -- N thing(s) to do[, note]." -- the doctor-style verdict."""
    if action_count <= 0:
        return "Ready."
    word = "thing" if action_count == 1 else "things"
    base = f"Not ready -- {action_count} {word} to do"
    if note:
        base = f"{base}, {note}"
    return base + "."


# ---------------------------------------------------------------------------
# ACTION band
# ---------------------------------------------------------------------------


def prose_lines(text: str, *, width: int = _DEFAULT_WIDTH) -> list[str]:
    """Prose in ACTION: column 2, wrapped at `width`."""
    avail = max(10, width - _ACTION_PROSE_COLUMN)
    out: list[str] = []
    for logical in text.splitlines() or [text]:
        out.extend(_wrap_logical_line(logical, avail))
    return [" " * _ACTION_PROSE_COLUMN + line for line in out]


@dataclass
class Decision:
    """A single ACTION decision: a contiguous command sequence, plus prose."""

    commands: list[str]
    prose: str | None = None  # max 2 lines


@dataclass
class Step:
    """One numbered step of a multi-step, cross-machine ACTION."""

    number: int
    intro: str  # step body text, e.g. "On Windows, in an Administrator PowerShell:"
    commands: list[str] = field(default_factory=list)
    note: str | None = None  # continuation prose specific to this step


@dataclass
class Action:
    decision: Decision | None = None
    steps: list[Step] = field(default_factory=list)
    overflow_note: str | None = None  # e.g. "2 more after this -- rerun doctor."


def _render_decision(decision: Decision, *, width: int) -> list[str]:
    lines: list[str] = []
    if decision.commands:
        lines.append("")
        for cmd in decision.commands:
            lines.append(" " * _ACTION_COMMAND_INDENT + cmd)
    if decision.prose:
        lines.append("")
        lines.extend(prose_lines(decision.prose, width=width))
    return lines


def _render_step(step: Step, *, width: int) -> list[str]:
    marker = f"{step.number}. "
    body_column = _ACTION_PROSE_COLUMN + len(marker)
    command_column = body_column + _ACTION_COMMAND_INDENT
    lines = [" " * _ACTION_PROSE_COLUMN + marker + step.intro]
    if step.commands:
        lines.append("")
        for cmd in step.commands:
            lines.append(" " * command_column + cmd)
    if step.note:
        lines.append("")
        for logical in step.note.splitlines() or [step.note]:
            lines.append(" " * body_column + logical)
    return lines


def render_action(action: Action, *, width: int = _DEFAULT_WIDTH) -> list[str]:
    """Render the full ACTION band, including its "Do this:" header line."""
    lines: list[str] = ["Do this:"]
    if action.decision is not None:
        lines.extend(_render_decision(action.decision, width=width))
    else:
        for index, step in enumerate(action.steps):
            if index > 0:
                lines.append("")
            lines.extend(_render_step(step, width=width))
    if action.overflow_note:
        lines.append("")
        lines.extend(prose_lines(action.overflow_note, width=width))
    return lines


# ---------------------------------------------------------------------------
# Top-level assembly
# ---------------------------------------------------------------------------


def render(
    verdict: str, state_lines: list[str], action_lines: list[str] | None = None
) -> str:
    """Assemble VERDICT / STATE / ACTION with exactly one blank-line separator

    between bands and a leading/trailing blank line as the only frame.
    Returns the full text -- callers should write it verbatim (e.g.
    `sys.stdout.write(...)`), not `print()` it (which would add a second
    trailing newline).
    """
    lines: list[str] = ["", verdict, "", *state_lines]
    if action_lines:
        lines += ["", *action_lines]
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Helper for callers extracting an embedded "run: <command>" suggestion out
# of an existing check message -- lets ACTION reuse guidance text that
# already exists in check functions instead of duplicating it.
# ---------------------------------------------------------------------------

_RUN_COMMAND_RE = re.compile(r"run:\s*(muxplex-deck[^\n.]*)")


def extract_run_command(message: str) -> str | None:
    match = _RUN_COMMAND_RE.search(message)
    if not match:
        return None
    return match.group(1).strip()
