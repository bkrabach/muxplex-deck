"""Attention-first session ordering: surface what's most likely urgent on page 1.

Reorders an already view-resolved (and possibly already `sort_order`-sorted)
session list into three stable tiers:

1. Sessions needing attention (`Bell.needs_attention`), newest bell fire first.
2. The active session, if it isn't already in tier 1.
3. Everything else, by `last_activity_at` descending (most recently active
   first, `None` last) when the server exposes that field -- otherwise the
   incoming base order is preserved unchanged.

Applied to the view-resolved list *before* paging (`interaction.Pager`), so
hot sessions land on the first page regardless of how many pages the view
has. This is sidecar-only client-side reordering -- muxplex's own
`sort_order` setting (alphabetical/manual) is untouched server-side; a
sidecar configured with `"sort": "server"` skips this module entirely and
shows exactly what `views.resolve_view` returned.
"""

from __future__ import annotations

from .client import Session

NEG_INF = float("-inf")


def activity_available(sessions: list[Session]) -> bool:
    """True if at least one session in `sessions` carries a `last_activity_at`.

    Servers predating the `feat/session-activity` branch never populate this
    field (every session's value is `None`) -- callers use this to decide
    whether to log the one-time "server doesn't expose this yet" notice and
    to select the tier-3 fallback ordering.
    """
    return any(s.last_activity_at is not None for s in sessions)


def apply_attention_sort(
    sessions: list[Session],
    active_session: str | None,
    *,
    activity_available: bool,
) -> list[Session]:
    """Reorder `sessions` (view-resolved, base-ordered) attention-first.

    Args:
        sessions: the view-resolved session list, in base order (whatever
            `views.resolve_view` produced -- alphabetical or server order).
        active_session: name of the currently active session, or None.
        activity_available: whether this connection's server exposes
            `last_activity_at` (see `activity_available()` above) -- pass
            the result of checking the *full* unfiltered session list, not
            just this view, since a view might legitimately contain zero
            sessions with recorded activity even on a server that supports
            the field.

    Returns:
        A new list in tier 1 -> tier 2 -> tier 3 order. Sessions not present
        in `sessions` (e.g. an active session outside this view) are not
        added -- this function only reorders what's already there.
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
    if activity_available:
        with_activity = [s for s in remaining if s.last_activity_at is not None]
        without_activity = [s for s in remaining if s.last_activity_at is None]
        with_activity.sort(key=lambda s: s.last_activity_at, reverse=True)  # type: ignore[arg-type,return-value]
        tier3 = with_activity + without_activity
    else:
        tier3 = remaining

    return tier1 + tier2 + tier3
