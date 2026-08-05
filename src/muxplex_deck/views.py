"""View resolution: mirrors muxplex's read-time visibility filter for the sidecar.

Ports `filter_visible` (muxplex/muxplex/views.py:41-106) so the deck always
shows the sessions belonging to the server's current view (`active_view`
from GET /api/state), in the same order the PWA would display them.

Verified against muxplex source:
- "all" -> every live session not in hidden_sessions.
- "hidden" -> only sessions in hidden_sessions.
- named view -> the server-resolved membership for that view (pins union
  glob-rule matches, see below), minus hidden_sessions.
- unknown view name -> empty list. This deliberately does NOT fall back to
  "all" -- an honest empty result (view name shown, 0 sessions) is correct
  per muxplex's own behavior; silently substituting a different view would
  misrepresent server state.
- sort_order == "alphabetical" sorts the result by name; any other value
  (default "manual") preserves GET /api/sessions order.

Named-view membership -- server-resolved, NOT re-derived (muxplex >=0.36.0):

muxplex v0.36.0 added auto-updating views: a view's `sessions` list (pins)
can be joined by a `match_names` glob-rule list, and membership is the union
of the two, resolved server-side on every read (see
`muxplex.views.annotate_view_membership` / `AUTO_VIEWS_SPEC.md` in the
workspace root). The server annotates every session dict from
`GET /api/sessions` with this resolved answer: `views: [<view name>, ...]`.
`muxplex_client.Session.views` carries it (`tuple[str, ...]`, default `()`).

**This module does NOT implement glob matching.** The matcher
(`fnmatch.fnmatchcase` + `.casefold()`) lives in exactly one place,
server-side, in muxplex itself -- that single-implementation property is
what keeps the PWA, the soft deck, and this sidecar from ever disagreeing
about what a rule-based view contains. Porting the matcher here would
recreate the exact bug this module now avoids (three independent
re-implementations silently drifting). `grep -rn fnmatch src/` must stay
empty in this repo.

Per-session fallback for pre-0.36.0 servers (deliberate, not incidental):

`_member_matches` (the old dual bare-name/"<device_id>:<name>"-suffix
search against a view's pins) is kept, but demoted to a **per-session
fallback**, not the primary path. For each session: if the server sent a
non-empty `views` tuple for it, that answer is authoritative (it already
reflects pins union rule-matches) and is used as-is. Only when a session's
`views` is empty do we fall back to `_member_matches` against the view's
pins.

Why per-session rather than "trust `session.views` unconditionally, no
fallback" (which is what the PWA does, see `AUTO_VIEWS_SPEC.md` §9.1):
the PWA is *served by* the server it polls, so a missing `views` field is
always a same-version bug there. This sidecar is not -- it is a standalone,
independently-versioned client that must keep working against a muxplex
that predates v0.36.0 entirely, where `views` is never sent and every
session's `.views` is permanently `()`. Trusting `session.views`
unconditionally would render every named view on such a server as
empty -- silently reproducing, for *every* view, the exact "rule-based view
renders empty" bug this change exists to fix (just for pin-based views
this time, which have nothing to do with the servers that lack this
capability). Falling back to `_member_matches` per-session instead makes
this sidecar correct on both sides of the v0.36.0 line: pin-based
membership works unconditionally, rule-based membership works whenever the
server resolves and sends it, and a genuinely rule-only view against an
old server renders empty for the same reason it would on that server's own
PWA (that server does not evaluate `match_names` at all) -- an honest
result, not a silently-broken one.

Membership matching (dual key form -- NOT bare-name-only), for the fallback:

muxplex runs a background normalization cycle (main.py's periodic poll,
"13b. Normalize bare session-key entries to the canonical device_id:name
form") that rewrites *every* view.sessions / hidden_sessions entry from a
bare name to "<device_id>:<name>" -- unconditionally, not just for
federation peers. This was verified empirically against a live server: a
view created via bare-name PATCH was rewritten to the device_id-prefixed
form within one poll cycle, with zero remote_instances configured. So in
practice, any view that has existed for more than an instant stores
"<device_id>:<name>", never the bare name.

GET /api/sessions (what this sidecar's client fetches) never returns a
sessionKey field for local sessions -- only bare `name` -- so exact/bare
matching against membership sets, as muxplex's own dual `sessionKey`/`name`
lookup does internally, is not available to us. Instead we match a session
by bare name OR by membership-entry suffix ":<name>" (tmux session names
cannot contain ':', so this suffix check is unambiguous).
"""

from __future__ import annotations

from muxplex_client import Session, Settings


def _member_matches(name: str, members: frozenset[str]) -> bool:
    """True if `name` is present in `members`, honoring both key forms.

    `members` (a view's `sessions` list, or `hidden_sessions`) may contain a
    bare session name (legacy) or the canonical "device_id:name" form muxplex
    normalizes to. Checks an exact bare-name match first (cheap, common case
    for freshly-created entries this sidecar's own connect calls might add),
    then falls back to a suffix check for the canonical form.
    """
    if name in members:
        return True
    suffix = f":{name}"
    return any(member.endswith(suffix) for member in members)


def _is_view_member(session: Session, view_name: str, pins: frozenset[str]) -> bool:
    """True if `session` belongs to the named view.

    Prefers the server-resolved answer (`session.views`, muxplex >=0.36.0:
    pins union glob-rule matches, annotated on every `GET /api/sessions`
    entry) -- this is the only path that can see rule-matched members, since
    the matcher never runs client-side (see module docstring).

    Falls back to `_member_matches` against `pins` (the view's `sessions`
    list) only when this session's `views` is empty -- either because the
    server predates v0.36.0 and never sends the field (permanently `()`
    for every session), or because the session genuinely belongs to no
    view (in which case the fallback also correctly finds no match, since
    an unpinned, unmatched session cannot appear in `pins` either). This
    keeps pin-based membership working unconditionally, on both sides of
    the v0.36.0 line, while rule-based membership works whenever the
    server resolves and sends it.
    """
    if session.views:
        return view_name in session.views
    return _member_matches(session.name, pins)


def resolve_view(
    sessions: list[Session], settings: Settings, active_view: str
) -> list[Session]:
    """Filter and sort `sessions` to what `active_view` shows.

    Args:
        sessions: live sessions from GET /api/sessions, in server order.
        settings: parsed GET /api/settings (views, hidden_sessions, sort_order).
        active_view: "all", "hidden", or a user view name, from GET /api/state.

    Returns:
        The visible sessions for `active_view`, sorted per `settings.sort_order`.
    """
    hidden = settings.hidden_sessions

    if active_view == "hidden":
        result = [s for s in sessions if _member_matches(s.name, hidden)]
    elif active_view == "all":
        result = [s for s in sessions if not _member_matches(s.name, hidden)]
    else:
        view = next((v for v in settings.views if v.name == active_view), None)
        if view is None:
            result = []
        else:
            result = [
                s
                for s in sessions
                if _is_view_member(s, active_view, view.sessions)
                and not _member_matches(s.name, hidden)
            ]

    if settings.sort_order == "alphabetical":
        result = sorted(result, key=lambda s: s.name)
    return result
