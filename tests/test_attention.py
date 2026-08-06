"""Tests for `attention.apply_attention_sort` (attention-first ordering).

No hardware, no server, no I/O -- pure list reordering exercised against
`muxplex_client.Session`/`Bell` value objects.

Covers:
- Tier 1: needs-attention sessions, newest bell fire first.
- Tier 2: everything else, ordered by `bell.last_fired_at` descending, with
  never-belled sessions sorting last and stable order preserved among ties.
- Regression guard: tier 2 must follow `bell.last_fired_at`, not
  `last_activity_at`, even when the two orders are deliberately opposed --
  this is the exact bug fixed in muxplex commit 3ed7490 and ported here.
- Regression guard: selecting a session must not change its position. A
  dedicated "active session" tier previously existed here (mirroring
  muxplex's v0.38.1, commit e7b3929) to fix "the session I'm working in
  sinks to the bottom" -- but that diagnosis was wrong. The real cause was
  muxplex's bell hook curling the wrong scheme at a TLS port, so bells never
  delivered for an attached session and its `bell.last_fired_at` froze;
  fixed server-side in the same release. With bells actually delivering,
  the actively-worked session rises on bell recency alone, and the tier is
  removed here to match main.py and app.js (all three must move together).
"""

from __future__ import annotations

from muxplex_client import Bell, Session

from muxplex_deck.attention import apply_attention_sort


def _session(
    name: str,
    *,
    last_fired_at: float | None = None,
    seen_at: float | None = None,
    unseen_count: int = 0,
    last_activity_at: float | None = None,
) -> Session:
    bell = Bell(last_fired_at=last_fired_at, seen_at=seen_at, unseen_count=unseen_count)
    return Session(name=name, snapshot="", bell=bell, last_activity_at=last_activity_at)


def test_tier1_needs_attention_sessions_first_newest_bell_first() -> None:
    a = _session("a", last_fired_at=100.0, seen_at=None, unseen_count=1)
    b = _session("b", last_fired_at=200.0, seen_at=None, unseen_count=1)
    c = _session("c")  # no bell activity at all -- not needs_attention

    result = apply_attention_sort([a, b, c])

    names = [s.name for s in result]
    assert names == ["b", "a", "c"]


def test_tier2_orders_by_bell_last_fired_at_descending() -> None:
    old_bell = _session("old-bell", last_fired_at=100.0, seen_at=100.0, unseen_count=0)
    recent_bell = _session(
        "recent-bell", last_fired_at=2000.0, seen_at=2000.0, unseen_count=0
    )
    never_belled = _session("never-belled")

    result = apply_attention_sort([never_belled, old_bell, recent_bell])

    names = [s.name for s in result]
    assert names == ["recent-bell", "old-bell", "never-belled"]


def test_tier2_regression_guard_follows_bell_not_last_activity_at() -> None:
    """Two non-attention sessions whose `last_activity_at` order is the
    OPPOSITE of their `bell.last_fired_at` order must be ordered by
    `bell.last_fired_at` -- the exact bug fixed in muxplex commit 3ed7490.
    """
    fresh_activity_old_bell = _session(
        "fresh-activity-old-bell",
        last_fired_at=100.0,
        seen_at=100.0,
        unseen_count=0,
        last_activity_at=2000.0,
    )
    old_activity_fresh_bell = _session(
        "old-activity-fresh-bell",
        last_fired_at=2000.0,
        seen_at=2000.0,
        unseen_count=0,
        last_activity_at=100.0,
    )

    result = apply_attention_sort([fresh_activity_old_bell, old_activity_fresh_bell])

    names = [s.name for s in result]
    # Must follow bell.last_fired_at (old-activity-fresh-bell first), NOT
    # last_activity_at (which would put fresh-activity-old-bell first).
    assert names == ["old-activity-fresh-bell", "fresh-activity-old-bell"]


def test_tier2_never_belled_sessions_sort_after_ever_belled_sessions() -> None:
    never_belled_1 = _session("never-belled-1")
    never_belled_2 = _session("never-belled-2")
    ever_belled = _session(
        "ever-belled", last_fired_at=1.0, seen_at=1.0, unseen_count=0
    )

    result = apply_attention_sort([never_belled_1, ever_belled, never_belled_2])

    names = [s.name for s in result]
    assert names == ["ever-belled", "never-belled-1", "never-belled-2"]


def test_tier2_ties_preserve_incoming_base_order_stable_sort() -> None:
    first = _session("first")
    second = _session("second")
    third = _session("third")

    result = apply_attention_sort([first, second, third])

    names = [s.name for s in result]
    assert names == ["first", "second", "third"]


def test_selecting_a_session_does_not_change_its_position() -> None:
    """Regression guard: there is no "active session" tier. Ordering must be
    identical whether or not a session in the list happens to be selected --
    the caller no longer even passes an active-session argument, but this
    also guards against a future reintroduction of one."""
    first = _session("first")
    middle = _session("middle")
    last = _session("last")

    baseline = [s.name for s in apply_attention_sort([first, middle, last])]

    # Re-run with a different "conceptual" active session each time (the
    # function no longer takes such a parameter at all -- the point of this
    # test is that the ORDER never depends on which session a caller
    # considers active).
    for _ in range(3):
        result = [s.name for s in apply_attention_sort([first, middle, last])]
        assert result == baseline == ["first", "middle", "last"]


def test_active_and_belled_session_ranked_by_bell_only_no_duplicate() -> None:
    """A session that would have been "active" is ordered purely by tier 1
    bell recency when it needs attention -- no tier to place it in twice."""
    bell_and_would_be_active = _session(
        "bell-active", last_fired_at=100.0, seen_at=None, unseen_count=1
    )
    other = _session("other")

    result = apply_attention_sort([other, bell_and_would_be_active])

    names = [s.name for s in result]
    assert names == ["bell-active", "other"]
    assert len(result) == 2
