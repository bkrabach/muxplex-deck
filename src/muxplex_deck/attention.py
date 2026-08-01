"""Attention-first session ordering: surface what's most likely urgent on page 1.

Reorders an already view-resolved (and possibly already `sort_order`-sorted)
session list into three stable tiers:

1. Sessions needing attention (`Bell.needs_attention`), newest bell fire first.
2. The active session, if it isn't already in tier 1.
3. Everything else, by `bell.last_fired_at` descending (sessions that have
   never belled sort last, preserving incoming base order among themselves).

Tier 3 deliberately keys off `bell.last_fired_at`, NOT `last_activity_at`:
that timestamp derives from tmux `#{window_activity}` and bumps on ANY pane
output (spinners, redraws, status-line clocks), so it reordered the grid on
essentially every ~2s poll cycle even with no real event to justify it. A
bell only fires on the actual agent-turn-completion signal, so keying tier 3
off it mirrors tier 1 and keeps ordering stable between bells -- the whole
point of an "attention" sort. See muxplex commit 3ed7490, which made the
identical change server-side (`main.py`'s `_attention_order()`) and in the
web client (`app.js`'s `sortByAttention()`) -- all three must move together.

Applied to the view-resolved list *before* paging (`interaction.Pager`), so
hot sessions land on the first page regardless of how many pages the view
has. This is sidecar-only client-side reordering -- muxplex's own
`sort_order` setting (alphabetical/manual) is untouched server-side; a
sidecar configured with `"sort": "server"` skips this module entirely and
shows exactly what `views.resolve_view` returned.
"""

from __future__ import annotations

from muxplex_client import Session

NEG_INF = float("-inf")


def apply_attention_sort(
    sessions: list[Session],
    active_session: str | None,
) -> list[Session]:
    """Reorder `sessions` (view-resolved, base-ordered) attention-first.

    Args:
        sessions: the view-resolved session list, in base order (whatever
            `views.resolve_view` produced -- alphabetical or server order).
        active_session: name of the currently active session, or None.

    Returns:
        A new list in tier 1 -> tier 2 -> tier 3 order. Sessions not present
        in `sessions` (e.g. an active session outside this view) are not
        added -- this function only reorders what's already there. Tier 3 is
        keyed off `bell.last_fired_at` descending (never-belled sessions
        last, stable among ties) -- see the module docstring for why.
    """
    tier1 = [s for s in sessions if s.bell.needs_attention]
    tier1.sort(key=lambda s: s.bell.last_fired_at or NEG_INF, reverse=True)
    tier1_names = {s.name for s in tier1}

    tier2 = [
        s for s in sessions if s.name == active_session and s.name not in tier1_names
    ]

    remaining = [
        s for s in sessions if s.name not in tier1_names and s.name != active_session
    ]
    tier3 = sorted(
        remaining,
        key=lambda s: (
            s.bell.last_fired_at is not None,
            s.bell.last_fired_at or NEG_INF,
        ),
        reverse=True,
    )

    return tier1 + tier2 + tier3
