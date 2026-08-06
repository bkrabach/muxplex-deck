"""Attention-first session ordering: surface what's most likely urgent on page 1.

Reorders an already view-resolved (and possibly already `sort_order`-sorted)
session list into two stable tiers:

1. Sessions needing attention (`Bell.needs_attention`), newest bell fire first.
2. Everything else, by `bell.last_fired_at` descending (sessions that have
   never belled sort last, preserving incoming base order among themselves).

Tier 2 deliberately keys off `bell.last_fired_at`, NOT `last_activity_at`:
that timestamp derives from tmux `#{window_activity}` and bumps on ANY pane
output (spinners, redraws, status-line clocks), so it reordered the grid on
essentially every ~2s poll cycle even with no real event to justify it. A
bell only fires on the actual agent-turn-completion signal, so keying tier 2
off it mirrors tier 1 and keeps ordering stable between bells -- the whole
point of an "attention" sort. See muxplex commit 3ed7490, which made the
identical change server-side (`main.py`'s `_attention_order()`) and in the
web client (`app.js`'s `sortByAttention()`) -- all three must move together.

There is deliberately NO separate "active session" tier. muxplex briefly
had one (v0.38.1, commit e7b3929) to fix "the session I'm working in sinks
to the bottom" -- but that diagnosis was wrong. The real cause was the
server's bell hook curling the wrong scheme at a TLS port, so bells never
delivered for an attached session and its `bell.last_fired_at` froze; fixed
server-side in the same release. With bells actually delivering, the
actively-worked session rises on bell recency alone, and a dedicated tier
is not just redundant -- it's wrong: it bumps a session because the user
SELECTED it, when this sort's whole contract is to track agent-turn-
completion events, not user navigation. It also masks bell-hook
regressions: if the hook breaks again, an active-session tier silently
props the session up and hides the symptom that would otherwise reveal it.
See muxplex's docs/API_SEMANTICS.md "?sort=attention" entry -- this ported
change removes the identical tier that main.py and app.js also removed, so
all three stay in agreement.

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


def apply_attention_sort(sessions: list[Session]) -> list[Session]:
    """Reorder `sessions` (view-resolved, base-ordered) attention-first.

    Args:
        sessions: the view-resolved session list, in base order (whatever
            `views.resolve_view` produced -- alphabetical or server order).

    Returns:
        A new list in tier 1 -> tier 2 order. Tier 2 is keyed off
        `bell.last_fired_at` descending (never-belled sessions last, stable
        among ties) -- see the module docstring for why. Selecting a
        session (which session is "active") has no effect on the result --
        see the module docstring for why there is no active-session tier.
    """
    tier1 = [s for s in sessions if s.bell.needs_attention]
    tier1.sort(key=lambda s: s.bell.last_fired_at or NEG_INF, reverse=True)
    tier1_names = {s.name for s in tier1}

    remaining = [s for s in sessions if s.name not in tier1_names]
    tier2 = sorted(
        remaining,
        key=lambda s: (
            s.bell.last_fired_at is not None,
            s.bell.last_fired_at or NEG_INF,
        ),
        reverse=True,
    )

    return tier1 + tier2
