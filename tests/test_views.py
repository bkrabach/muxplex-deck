"""Pure tests for `views.resolve_view` -- no I/O, no device, no network.

Covers the muxplex >=0.36.0 rule-based (auto-updating) view fix: named-view
membership must prefer the server-resolved `Session.views` annotation (pins
union glob-rule matches) over the old client-side pin-only derivation, while
still falling back to the pin-only derivation for a session the server sent
no `views` annotation for (a pre-0.36.0 server, or -- equivalently correct --
a session that is genuinely a member of nothing).
"""

from __future__ import annotations

from muxplex_client import Bell, Session, Settings, View

from muxplex_deck.views import resolve_view

NO_BELL = Bell(last_fired_at=None, seen_at=None, unseen_count=0)


def make_session(name: str, views: tuple[str, ...] = ()) -> Session:
    return Session(name=name, snapshot="", bell=NO_BELL, views=views)


def make_settings(
    views: tuple[View, ...] = (),
    hidden_sessions: frozenset[str] = frozenset(),
    sort_order: str = "manual",
) -> Settings:
    return Settings(views=views, hidden_sessions=hidden_sessions, sort_order=sort_order)


class TestAllAndHidden:
    """Pseudo-views are resolved from hidden_sessions, unaffected by this change."""

    def test_all_excludes_hidden(self) -> None:
        sessions = [make_session("alpha"), make_session("beta")]
        settings = make_settings(hidden_sessions=frozenset({"beta"}))
        result = resolve_view(sessions, settings, "all")
        assert [s.name for s in result] == ["alpha"]

    def test_hidden_shows_only_hidden(self) -> None:
        sessions = [make_session("alpha"), make_session("beta")]
        settings = make_settings(hidden_sessions=frozenset({"beta"}))
        result = resolve_view(sessions, settings, "hidden")
        assert [s.name for s in result] == ["beta"]

    def test_unknown_view_is_empty_not_all(self) -> None:
        sessions = [make_session("alpha")]
        settings = make_settings(views=(View(name="Work", sessions=frozenset()),))
        result = resolve_view(sessions, settings, "does-not-exist")
        assert result == []


class TestPinOnlyMembership:
    """Regression: pin-based views must keep working byte-identically."""

    def test_pinned_session_included_via_fallback(self) -> None:
        # Old-server shape: sessions carry no `views` annotation at all.
        sessions = [make_session("alpha"), make_session("beta")]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset({"alpha"})),)
        )
        result = resolve_view(sessions, settings, "Work")
        assert [s.name for s in result] == ["alpha"]

    def test_pinned_session_via_device_qualified_suffix(self) -> None:
        sessions = [make_session("alpha")]
        settings = make_settings(
            views=(
                View(
                    name="Work",
                    sessions=frozenset({"d502b663-1f0a-4c8e-9c31-2f1b0a77b6de:alpha"}),
                ),
            )
        )
        result = resolve_view(sessions, settings, "Work")
        assert [s.name for s in result] == ["alpha"]

    def test_non_member_excluded(self) -> None:
        sessions = [make_session("alpha"), make_session("beta")]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset({"alpha"})),)
        )
        result = resolve_view(sessions, settings, "Work")
        assert "beta" not in [s.name for s in result]

    def test_pinned_but_hidden_is_excluded(self) -> None:
        sessions = [make_session("alpha")]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset({"alpha"})),),
            hidden_sessions=frozenset({"alpha"}),
        )
        result = resolve_view(sessions, settings, "Work")
        assert result == []


class TestRuleMatchedMembership:
    """The actual fix: a session matched by a server-side glob rule (no pin)
    must appear -- this is the exact case that rendered EMPTY on v0.12.1.
    """

    def test_rule_matched_session_with_no_pin_is_included(self) -> None:
        # "Work" has zero pins -- on the old code path this view is
        # unconditionally empty. The server nonetheless resolved
        # `amplifier-agent` into the view via a `match_names` rule and
        # annotated it on the session -- this must now surface it.
        sessions = [make_session("amplifier-agent", views=("Work",))]
        settings = make_settings(views=(View(name="Work", sessions=frozenset()),))
        result = resolve_view(sessions, settings, "Work")
        assert [s.name for s in result] == ["amplifier-agent"]

    def test_rule_matched_session_not_in_pins_list_still_included(self) -> None:
        # The view has pins for OTHER sessions, but "amplifier-agent" was
        # never pinned -- only server-resolved membership can find it.
        sessions = [
            make_session("pinned-one", views=("Work",)),
            make_session("amplifier-agent", views=("Work",)),
        ]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset({"pinned-one"})),)
        )
        result = resolve_view(sessions, settings, "Work")
        assert {s.name for s in result} == {"pinned-one", "amplifier-agent"}

    def test_union_of_pin_and_rule_no_duplicates(self) -> None:
        # A session that is BOTH pinned and rule-matched appears exactly once.
        sessions = [make_session("amplifier-agent", views=("Work",))]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset({"amplifier-agent"})),)
        )
        result = resolve_view(sessions, settings, "Work")
        assert [s.name for s in result] == ["amplifier-agent"]

    def test_session_in_other_views_only_is_excluded(self) -> None:
        sessions = [make_session("other-agent", views=("Play",))]
        settings = make_settings(views=(View(name="Work", sessions=frozenset()),))
        result = resolve_view(sessions, settings, "Work")
        assert result == []

    def test_rule_matched_but_hidden_is_excluded(self) -> None:
        sessions = [make_session("amplifier-agent", views=("Work",))]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset()),),
            hidden_sessions=frozenset({"amplifier-agent"}),
        )
        result = resolve_view(sessions, settings, "Work")
        assert result == []


class TestMixedFleet:
    """A session's own `.views` can be authoritative while a sibling session
    (e.g. one the server genuinely resolved to no memberships) falls back --
    both must be handled correctly in the same call.
    """

    def test_authoritative_and_fallback_sessions_together(self) -> None:
        sessions = [
            make_session("rule-hit", views=("Work",)),  # new server, matched
            make_session("pinned-only"),  # views=() -> fallback to pins
            make_session("elsewhere", views=("Play",)),  # new server, not a member
        ]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset({"pinned-only"})),)
        )
        result = resolve_view(sessions, settings, "Work")
        assert {s.name for s in result} == {"rule-hit", "pinned-only"}


class TestSortOrder:
    def test_alphabetical_sorts_named_view(self) -> None:
        sessions = [
            make_session("zeta", views=("Work",)),
            make_session("alpha", views=("Work",)),
        ]
        settings = make_settings(
            views=(View(name="Work", sessions=frozenset()),), sort_order="alphabetical"
        )
        result = resolve_view(sessions, settings, "Work")
        assert [s.name for s in result] == ["alpha", "zeta"]

    def test_manual_preserves_server_order(self) -> None:
        sessions = [
            make_session("zeta", views=("Work",)),
            make_session("alpha", views=("Work",)),
        ]
        settings = make_settings(views=(View(name="Work", sessions=frozenset()),))
        result = resolve_view(sessions, settings, "Work")
        assert [s.name for s in result] == ["zeta", "alpha"]
