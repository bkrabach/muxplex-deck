"""v2 federation-aware deck rendering (physical deck half).

`muxplex/docs/plans/2026-08-16-deck-control-target-design.md` §4.5 v2, the
follow-up scoped as future work by Step 5: "poll `/api/federation/sessions`,
carry `remoteId` through `Session`, connect via the federation proxy -- a
rewrite of the deck's fetch/render/connect path."

Four things, kept in one file since they're one feature:

1. The poll loop (`_ActiveRuntime.refresh`) now calls `federation_sessions()`
   instead of `sessions()` -- the merged local+remote list populates
   `self.ordered` exactly like `sessions()` used to, and a server with no
   configured federation peers (the common case, and every pre-v2 test's
   `FakeClient`) is byte-identical.
2. Rendering: a remote session tile gets a STATE-band origin label (its
   device_name); a local one renders exactly as before (no origin_label).
   Same-named local+remote entries key distinctly by `remote_id`/
   `session_key`, never conflated.
3. Connect routing: pressing a local slot's key calls
   `client.connect(name, device_id=...)`; pressing a remote slot's key
   calls `client.connect(name, remote_id=...)` -- resolved from the
   pressed `Session` itself (`page_sessions`), never guessed from the
   bare name.
4. Failure handling: an unreachable/auth_failed federation peer surfaces as
   a `RemoteStatus` entry (never a crash, never a silently shorter list) --
   logged once per change and (on a deck with a strip) summarized on it.

Uses the same `FakeDeck`/`FakeClient` fakes as test_runtime_modes.py/
test_target_picker.py (no hardware, no server, no real threads left
dangling). `FakeClient.federation_sessions()`/`.statuses`/
`.connect_remote_ids` are v2's own additions to that shared fixture (see
test_runtime_modes.py).
"""

from __future__ import annotations

from typing import cast

from muxplex_client import Bell, MuxplexClient, RemoteStatus, Session
from test_runtime_modes import SETTINGS, FakeClient, FakeDeck, _make_sessions

from muxplex_deck.device import DeckDevice
from muxplex_deck.interaction import ViewCycler
from muxplex_deck.main import _ActiveRuntime

_TEST_DEBOUNCE_SECONDS = 0.05
_WAIT_SECONDS = 5.0

_BELL = Bell(last_fired_at=None, seen_at=None, unseen_count=0)


def _make_full_deck() -> FakeDeck:
    return FakeDeck(
        key_count=8, key_layout=(2, 4), key_size=(120, 120), dial_count=4, is_touch=True
    )


def _make_reduced_deck() -> FakeDeck:
    """15-key Original-class deck -- no dials, no touch strip.

    Used to prove federation peer-status degradation is still visible
    (via logging/status.json, not just the strip) on the exact hardware
    class this feature ships to first (see the parent conversation: the
    owner's actual Stream Deck is an Original).
    """
    return FakeDeck(
        key_count=15, key_layout=(3, 5), key_size=(72, 72), dial_count=0, is_touch=False
    )


def _make_runtime(
    deck: FakeDeck,
    client: FakeClient,
    *,
    device_id: str = "d-self",
    controls: dict[str, str] | None = None,
) -> _ActiveRuntime:
    ctx = _ActiveRuntime(
        deck=cast(DeckDevice, deck),
        client=cast(MuxplexClient, client),
        hostname="test-server",
        sort_mode="server",
        device_id=device_id,
        controls=controls or {},
    )
    ctx.view_cycler = ViewCycler(debounce_seconds=_TEST_DEBOUNCE_SECONDS)
    return ctx


def _remote_session(
    name: str,
    *,
    remote_id: str,
    device_name: str,
    device_id: str | None = None,
) -> Session:
    device_id = device_id or remote_id
    return Session(
        name=name,
        snapshot="remote output\n",
        bell=_BELL,
        device_id=device_id,
        device_name=device_name,
        remote_id=remote_id,
        session_key=f"{device_id}:{name}",
    )


# ---------------------------------------------------------------------------
# 1. Poll loop: federation_sessions() replaces sessions(), byte-identical
#    when unused (no peers / no remote entries).
# ---------------------------------------------------------------------------


class TestPollLoopByteIdenticalWhenNoFederationPeers:
    def test_local_only_sessions_populate_ordered_exactly_as_before(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        assert [s.name for s in ctx.ordered] == [
            "session-00",
            "session-01",
            "session-02",
            "session-03",
            "session-04",
        ]
        assert ctx.remote_statuses == ()
        # Every rendered key for a purely-local deck is a plain session
        # tile -- no STATE-band origin label painted (see rendering test
        # below for the direct pixel-level proof).
        assert client.connected_names == []

    def test_session_key_press_still_connects_local_via_device_id(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-abc")

        ctx.refresh()
        ctx.handle_key(2)

        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-02"]
        assert client.connect_device_ids == ["d-abc"]
        assert client.connect_remote_ids == [None]


# ---------------------------------------------------------------------------
# 2. Federation-aware session list: local + remote merged, uniquely keyed.
# ---------------------------------------------------------------------------


class TestFederationAwareSessionList:
    def test_merged_local_and_remote_sessions_both_appear(self) -> None:
        sessions = [
            *_make_sessions(2),
            _remote_session("web", remote_id="d-mbp", device_name="MacBook"),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        names = [s.name for s in ctx.ordered]
        assert names == ["session-00", "session-01", "web"]
        remote_entry = ctx.ordered[2]
        assert remote_entry.remote_id == "d-mbp"
        assert remote_entry.device_name == "MacBook"

    def test_same_named_local_and_remote_sessions_key_distinctly(self) -> None:
        """The exact hazard `session_key`/`remote_id` exist to resolve

        (already known from Step 5's own work): two servers can each have
        a session named "dotfiles". Both must appear, distinguishably,
        never merged/collapsed into one.
        """
        sessions = [
            Session(name="dotfiles", snapshot="local\n", bell=_BELL),
            _remote_session("dotfiles", remote_id="d-mbp", device_name="MacBook"),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        assert len(ctx.ordered) == 2
        assert ctx.ordered[0].remote_id is None
        assert ctx.ordered[1].remote_id == "d-mbp"
        # session_key is the real, globally-unique identity -- distinct
        # even though `.name` collides.
        assert ctx.ordered[0].session_key != ctx.ordered[1].session_key


# ---------------------------------------------------------------------------
# 3. Rendering: STATE-band origin label distinguishes a remote tile.
# ---------------------------------------------------------------------------


class TestRemoteSessionRendering:
    def test_local_session_tile_has_no_origin_label(self) -> None:
        """Byte-identical rendering for a local session (no origin_label

        passed to `render_session_key`) -- proven indirectly via the
        identity cache: a session with `remote_id=None`/`device_name=None`
        paints under an identity tuple whose origin fields are both None.
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(3), SETTINGS)
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        identity = cast(tuple, ctx.last_key_state[0])
        # (name, remote_id, device_name, active, needs_attention, snapshot)
        assert identity[0] == "session-00"
        assert identity[1] is None
        assert identity[2] is None

    def test_remote_session_tile_identity_carries_its_origin(self) -> None:
        sessions = [_remote_session("web", remote_id="d-mbp", device_name="MacBook")]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        identity = cast(tuple, ctx.last_key_state[0])
        assert identity[0] == "web"
        assert identity[1] == "d-mbp"
        assert identity[2] == "MacBook"

    def test_remote_tile_falls_back_to_remote_id_when_device_name_missing(self) -> None:
        """`_session_origin_label`'s honest fallback: a peer's own

        `deviceName` can be empty/omitted (an older server, or a peer
        that never set `device_name` in its settings) -- the tile must
        still show SOMETHING distinguishing, never a blank STATE band
        for a session that IS remote (that would silently reintroduce
        the exact collision hazard this whole feature exists to fix).
        """
        from muxplex_deck.main import _session_origin_label

        session = _remote_session("web", remote_id="d-mbp", device_name="")
        assert _session_origin_label(session) == "d-mbp"

        local_session = Session(name="web", snapshot="", bell=_BELL)
        assert _session_origin_label(local_session) is None


# ---------------------------------------------------------------------------
# 3 (cont'd). Connect routing: local vs. federation-proxy, resolved from
# the pressed Session itself -- never guessed from the bare name.
# ---------------------------------------------------------------------------


class TestConnectRouting:
    def test_pressing_a_remote_slot_routes_through_remote_id(self) -> None:
        sessions = [
            *_make_sessions(1),
            _remote_session("web", remote_id="d-mbp", device_name="MacBook"),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-self")

        ctx.refresh()
        ctx.handle_key(1)  # slot 1 -> the remote "web" session

        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["web"]
        assert client.connect_remote_ids == ["d-mbp"]
        # Mutually exclusive with device_id on the real client -- a
        # remote press must never also carry this deck's own device_id.
        assert client.connect_device_ids == [None]

    def test_pressing_a_local_slot_routes_through_device_id_not_remote(self) -> None:
        sessions = [
            _remote_session("web", remote_id="d-mbp", device_name="MacBook"),
            *_make_sessions(1),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-self")

        ctx.refresh()
        ctx.handle_key(1)  # slot 1 -> the LOCAL "session-00"

        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-00"]
        assert client.connect_device_ids == ["d-self"]
        assert client.connect_remote_ids == [None]

    def test_same_named_local_and_remote_presses_connect_the_right_one(self) -> None:
        """The direct proof of the collision hazard being closed: two

        same-named entries, at two different keys, each press must reach
        ITS OWN session -- never the other one, whether local or remote.
        """
        sessions = [
            Session(name="dotfiles", snapshot="local\n", bell=_BELL),
            _remote_session("dotfiles", remote_id="d-mbp", device_name="MacBook"),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-self")
        ctx.refresh()

        ctx.handle_key(0)  # the LOCAL "dotfiles"
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connect_remote_ids == [None]
        assert client.connect_device_ids == ["d-self"]
        client.connect_event.clear()

        ctx.handle_key(1)  # the REMOTE "dotfiles"
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connect_remote_ids == [None, "d-mbp"]
        assert client.connect_device_ids == ["d-self", None]

    def test_toggle_last_reconnects_previous_remote_session_correctly(self) -> None:
        """`_toggle_last` (pre-existing feature) must route a remembered

        REMOTE previous session through the same remote-aware connect --
        not silently downgrade it to a local connect attempt. Exercises
        `previous_remote_id`, the federation-aware companion to
        `previous_session` (see `_note_active_session_locked`'s docstring).
        """
        sessions = [
            Session(name="local-a", snapshot="", bell=_BELL),
            _remote_session("web", remote_id="d-mbp", device_name="MacBook"),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        ctx = _make_runtime(deck, client, controls={"key.7": "toggle_last"})  # type: ignore[call-arg]
        ctx.refresh()

        ctx.handle_key(1)  # connect the remote "web" first
        assert client.connect_event.wait(_WAIT_SECONDS)
        client.connect_event.clear()

        ctx.handle_key(0)  # then the local "local-a" -- displaces "web"
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert ctx.previous_session == "web"
        assert ctx.previous_remote_id == "d-mbp"
        client.connect_event.clear()

        ctx.handle_key(7)  # TOGGLE -> back to "web", via remote_id
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names[-1] == "web"
        assert client.connect_remote_ids[-1] == "d-mbp"


# ---------------------------------------------------------------------------
# 4. Failure handling: unreachable/auth_failed peers -- visible, never a
#    crash, never a silent omission.
# ---------------------------------------------------------------------------


class TestFederationPeerDegradation:
    def test_degraded_peer_status_is_recorded_not_dropped(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(3), SETTINGS)
        client.statuses = (
            RemoteStatus(
                device_id="d-alienware",
                remote_id="d-alienware",
                device_name="alienware-r13",
                status="unreachable",
            ),
        )
        ctx = _make_runtime(deck, client)

        ctx.refresh()  # must not raise

        assert len(ctx.remote_statuses) == 1
        assert ctx.remote_statuses[0].status == "unreachable"
        # The rest of the poll cycle still completed normally -- sessions
        # rendered, no crash, no shortened list silently swallowed.
        assert len(ctx.ordered) == 3

    def test_degraded_peer_summarized_on_the_strip_when_present(self) -> None:
        deck = _make_full_deck()  # Stream Deck+ -- has a strip
        client = FakeClient(_make_sessions(2), SETTINGS)
        client.statuses = (
            RemoteStatus(
                device_id="d-alienware",
                remote_id="d-alienware",
                device_name="alienware-r13",
                status="unreachable",
            ),
        )
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        strip = ctx.last_strip
        assert strip is not None
        assert "alienware-r13: unreachable" in strip

    def test_multiple_degraded_peers_summarized_as_a_count(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(2), SETTINGS)
        client.statuses = (
            RemoteStatus(
                device_id="d-a", remote_id="d-a", device_name="a", status="unreachable"
            ),
            RemoteStatus(
                device_id="d-b", remote_id="d-b", device_name="b", status="auth_failed"
            ),
        )
        ctx = _make_runtime(deck, client)

        ctx.refresh()

        strip = ctx.last_strip
        assert strip is not None
        assert "2 peers degraded" in strip

    def test_recovering_peer_clears_the_strip_summary(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(2), SETTINGS)
        client.statuses = (
            RemoteStatus(
                device_id="d-a", remote_id="d-a", device_name="a", status="unreachable"
            ),
        )
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert "a: unreachable" in cast(str, ctx.last_strip)

        client.statuses = ()
        ctx.refresh()

        assert "a: unreachable" not in cast(str, ctx.last_strip)
        assert ctx.remote_statuses == ()

    def test_degraded_peer_visible_via_status_json_on_a_strip_less_deck(self) -> None:
        """The Original/MK2/XL/Mini class (this feature's actual first

        hardware target, per the owner's own deck) has no strip at all --
        `remote_statuses` must still be inspectable via `status.json`
        (`main._remote_statuses_for_status`), not silently dropped just
        because there's nowhere on the physical face to show it.
        """
        from muxplex_deck.main import _remote_statuses_for_status

        deck = _make_reduced_deck()
        client = FakeClient(_make_sessions(3), SETTINGS)
        client.statuses = (
            RemoteStatus(
                device_id="d-a",
                remote_id="d-a",
                device_name="alienware-r13",
                status="unreachable",
            ),
        )
        ctx = _make_runtime(deck, client)

        ctx.refresh()  # must not raise, even with no strip to summarize onto

        assert ctx.plan.use_strip is False
        published = _remote_statuses_for_status(ctx.remote_statuses)
        assert published == [
            {
                "device_id": "d-a",
                "remote_id": "d-a",
                "device_name": "alienware-r13",
                "status": "unreachable",
                "device_version": None,
            }
        ]

    def test_empty_statuses_publish_as_none_not_an_empty_list(self) -> None:
        from muxplex_deck.main import _remote_statuses_for_status

        assert _remote_statuses_for_status(()) is None
