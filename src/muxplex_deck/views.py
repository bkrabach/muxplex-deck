"""View resolution: mirrors muxplex's read-time visibility filter for the sidecar.

Ports `filter_visible` (muxplex/muxplex/views.py:41-106) so the deck always
shows the sessions belonging to the server's current view (`active_view`
from GET /api/state), in the same order the PWA would display them.

Verified against muxplex source:
- "all" -> every live session not in hidden_sessions.
- "hidden" -> only sessions in hidden_sessions.
- named view -> sessions whose name is in that view's membership, minus
  hidden_sessions.
- unknown view name -> empty list. This deliberately does NOT fall back to
  "all" -- an honest empty result (view name shown, 0 sessions) is correct
  per muxplex's own behavior; silently substituting a different view would
  misrepresent server state.
- sort_order == "alphabetical" sorts the result by name; any other value
  (default "manual") preserves GET /api/sessions order.

Membership matching (dual key form -- NOT bare-name-only):

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
                if _member_matches(s.name, view.sessions)
                and not _member_matches(s.name, hidden)
            ]

    if settings.sort_order == "alphabetical":
        result = sorted(result, key=lambda s: s.name)
    return result
