"""Step 5 (physical deck) of

`muxplex/docs/plans/2026-08-16-deck-control-target-design.md` §7.2/§9.2/§10:
strip target indicator + ship-blocking remote-session suppression + the
opt-in `target_picker` action / `PickerMode.TARGET`.

Three things, kept in one file since they're one feature:

1. `_target_indicator_text`/`_build_strip_message` -- pure composition of the
   strip's trailing "> ..." segment across every state (shared/paired/
   remote-degraded/local-only, with and without a view pin).
2. The §4.5/§7.2 ship-blocker: `active_remote_id` non-null suppresses the
   active-session highlight (keys AND the "ACTIVE: x" strip text) and blocks
   the picker.
3. `target_picker` + `PickerMode.TARGET`: option set (escape hatches + local
   devices, no federated entries), selection/commit, sticky resend on every
   heartbeat, and the `TargetGoneError`/`TargetNotSelfOwningError`
   degrade-sticky-and-visible fallback.

Uses the same `FakeDeck`/`FakeClient` fakes as test_runtime_modes.py/
test_device_identity.py/test_view_pin.py (no hardware, no server, no real
threads left dangling). `FakeClient.devices`/`FakeClient.heartbeat_error`
are this file's own additions to that shared fixture (see
test_runtime_modes.py).
"""

from __future__ import annotations

from typing import cast

from muxplex_client import (
    Bell,
    MuxplexClient,
    Session,
    TargetGoneError,
    TargetNotSelfOwningError,
)
from test_runtime_modes import SETTINGS, FakeClient, FakeDeck, _make_sessions

from muxplex_deck.device import DeckDevice
from muxplex_deck.interaction import PickerMode, ViewCycler
from muxplex_deck.main import (
    TARGET_LOCAL,
    TARGET_SHARED,
    _ActiveRuntime,
    _build_strip_message,
    _target_indicator_text,
)

_TEST_DEBOUNCE_SECONDS = 0.05
_WAIT_SECONDS = 5.0


def _make_full_deck() -> FakeDeck:
    return FakeDeck(
        key_count=8, key_layout=(2, 4), key_size=(120, 120), dial_count=4, is_touch=True
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


# ---------------------------------------------------------------------------
# Pure composition: `_target_indicator_text` / `_build_strip_message`
# ---------------------------------------------------------------------------


class TestTargetIndicatorTextPrecedence:
    def test_untouched_default_reads_shared(self) -> None:
        assert (
            _target_indicator_text(target_selection=None, active_remote_id=None)
            == "> shared"
        )

    def test_explicit_shared_reads_shared(self) -> None:
        assert (
            _target_indicator_text(
                target_selection=TARGET_SHARED, active_remote_id=None
            )
            == "> shared"
        )

    def test_local_reads_local(self) -> None:
        assert (
            _target_indicator_text(target_selection=TARGET_LOCAL, active_remote_id=None)
            == "> local"
        )

    def test_paired_device_uses_resolved_label(self) -> None:
        text = _target_indicator_text(
            target_selection="device:d-alienware",
            active_remote_id=None,
            target_label="Stream Deck (alienware)",
        )
        assert text == "> Stream Deck (alienware)"

    def test_paired_device_falls_back_to_raw_value_when_label_unknown(self) -> None:
        text = _target_indicator_text(
            target_selection="device:d-alienware", active_remote_id=None
        )
        assert text == "> device:d-alienware"

    def test_remote_degraded_overrides_shared(self) -> None:
        text = _target_indicator_text(
            target_selection=TARGET_SHARED, active_remote_id="remote-uuid"
        )
        assert text == "> remote (remote-uuid)"

    def test_remote_degraded_overrides_paired(self) -> None:
        text = _target_indicator_text(
            target_selection="device:d-alienware",
            active_remote_id="remote-uuid",
            target_label="Stream Deck (alienware)",
        )
        assert text == "> remote (remote-uuid)"

    def test_local_overrides_even_a_stale_remote_id(self) -> None:
        """ "Just me" is explicit and always wins -- see the function's own

        docstring on why a frozen/stale `active_remote_id` must never
        re-surface once the user has said "ignore the group entirely".
        """
        text = _target_indicator_text(
            target_selection=TARGET_LOCAL, active_remote_id="remote-uuid"
        )
        assert text == "> local"


class TestBuildStripMessageComposesTarget:
    def _base_kwargs(self) -> dict:
        return {
            "view_label": "all",
            "turning": False,
            "page": 1,
            "page_count": 1,
            "hostname": "spark-1",
            "total": 12,
            "active_session": "foo",
        }

    def test_no_target_text_is_byte_identical_to_before(self) -> None:
        message = _build_strip_message(**self._base_kwargs())
        assert message == "all \u00b7 spark-1 \u00b7 12 sessions \u00b7 ACTIVE: foo"

    def test_target_text_appended_at_the_end(self) -> None:
        message = _build_strip_message(**self._base_kwargs(), target_text="> shared")
        assert message == (
            "all \u00b7 spark-1 \u00b7 12 sessions \u00b7 ACTIVE: foo \u00b7 > shared"
        )

    def test_pin_and_target_compose_together(self) -> None:
        kwargs = self._base_kwargs()
        kwargs["pinned"] = True
        message = _build_strip_message(**kwargs, target_text="> MacBook")
        assert message == (
            "[all] \u00b7 spark-1 \u00b7 12 sessions \u00b7 ACTIVE: foo \u00b7 > MacBook"
        )


# ---------------------------------------------------------------------------
# §4.5/§7.2 ship-blocker, generalized by v2 federation-aware rendering
# (design doc §4.5): a same-named LOCAL session must never be painted/
# treated as active when the resolved group's actual active session is a
# DIFFERENT (remote) one -- see `_paint_keys`'s own docstring and
# `active_remote_id`'s docstring on `_ActiveRuntime.__init__` for the full
# reasoning this class now exercises. Step 5's original fix was a blanket
# "hide the name/highlight whenever active_remote_id is set"; v2 replaces
# that with per-entry (name, remote_id) matching, which fixes the SAME
# hazard while also correctly highlighting/connecting the true remote
# entry when it's actually present in this deck's own (now
# federation-aware) session list. `TestRemoteSessionSuppression` below
# covers the "not present" (still-safe) case; `TestRemoteSessionResolved`
# covers the new "present, now correctly resolved" case.
# ---------------------------------------------------------------------------


class TestRemoteSessionSuppression:
    """The remote entry is NOT present in this deck's own session list this
    tick (e.g. excluded from the merged federation fetch) -- the safe
    fallback Step 5 always had: no key highlights as active. Uses plain
    `_make_sessions()` (all `remote_id=None`), so `self.ordered` here never
    contains an entry matching `active_remote_id` at all.
    """

    def test_key_highlight_not_active_when_remote_entry_absent(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(8), SETTINGS)
        client.active_session = "session-03"
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        # Sanity: without the hazard, session-03's key IS painted active
        # (active_remote_id is None here, matching its own remote_id=None).
        identity = cast(tuple, ctx.last_key_state[3])
        assert identity[3] is True  # (name, remote_id, device_name, active, ...)

        client.active_remote_id = "remote-uuid"
        ctx.refresh()

        # session-03 is LOCAL (remote_id=None); active_remote_id is now
        # "remote-uuid" -- the two can never match, so this same key is
        # correctly no longer active, even though its NAME still matches
        # ctx.active_session. This is the structural fix, not a lookup.
        identity = cast(tuple, ctx.last_key_state[3])
        assert identity[3] is False
        # The underlying tracked state is NOT corrupted -- only the paint
        # doesn't match. toggle_last/previous_session bookkeeping must
        # still see the real value.
        assert ctx.active_session == "session-03"
        assert ctx.active_remote_id == "remote-uuid"

    def test_strip_shows_true_active_name_plus_remote_indicator(self) -> None:
        """v2 change from the old blanket suppression (see class docstring):

        the strip's "ACTIVE: x" text is no longer forced to "none" --
        it's read-only display with no connect action behind it, and the
        real mis-connect hazard is now closed at the matching layer
        (`_paint_keys`), not by hiding the name. The "> remote (...)"
        target segment still marks it as non-local.
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(8), SETTINGS)
        client.active_session = "session-03"
        client.active_remote_id = "remote-uuid"
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        strip = ctx.last_strip
        assert strip is not None
        assert "ACTIVE: session-03" in strip
        assert "> remote (remote-uuid)" in strip


class TestRemoteSessionResolved:
    """v2 federation-aware rendering (design doc §4.5): the resolved

    group's active session IS present in this deck's own (now
    federation-aware) `self.ordered` -- the new case Step 5's original
    fix could never reach, because its own session list was local-only.
    """

    def test_matching_remote_entry_is_highlighted_active(self) -> None:
        bell = Bell(last_fired_at=None, seen_at=None, unseen_count=0)
        sessions = [
            # A LOCAL session sharing the exact same name as the remote
            # one below -- the collision `session_key`/`remote_id` exist
            # to resolve (see `Session`'s own docstring).
            Session(name="dotfiles", snapshot="local\n", bell=bell),
            Session(
                name="dotfiles",
                snapshot="remote\n",
                bell=bell,
                device_id="d-mbp",
                device_name="MacBook",
                remote_id="d-mbp",
                session_key="d-mbp:dotfiles",
            ),
        ]
        deck = _make_full_deck()
        client = FakeClient(sessions, SETTINGS)
        client.active_session = "dotfiles"
        client.active_remote_id = "d-mbp"
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        # Slot 0 (the LOCAL "dotfiles") must NOT be active -- it shares a
        # name with the true active session but not its remote_id.
        local_identity = cast(tuple, ctx.last_key_state[0])
        assert local_identity[3] is False
        # Slot 1 (the REMOTE "dotfiles", peer "d-mbp") IS the true active
        # session and is now correctly resolvable/highlighted.
        remote_identity = cast(tuple, ctx.last_key_state[1])
        assert remote_identity[3] is True

        strip = ctx.last_strip
        assert strip is not None
        assert "ACTIVE: dotfiles" in strip
        assert "> remote (d-mbp)" in strip

    def test_target_picker_blocked_while_remote_degraded(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(8), SETTINGS)
        client.active_remote_id = "remote-uuid"
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        ctx.handle_key(0)

        assert ctx.picker.mode == PickerMode.NONE

    def test_target_picker_available_once_remote_state_clears(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(8), SETTINGS)
        client.active_remote_id = "remote-uuid"
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()
        ctx.handle_key(0)
        assert ctx.picker.mode == PickerMode.NONE

        client.active_remote_id = None
        ctx.refresh()
        ctx.handle_key(0)

        assert ctx.picker.mode == PickerMode.TARGET


# ---------------------------------------------------------------------------
# target_picker option set: escape hatches + local devices, no federated
# ---------------------------------------------------------------------------


class TestTargetOptions:
    def test_default_registry_is_just_the_two_escape_hatches(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        assert ctx._target_options() == [
            (TARGET_SHARED, "Shared"),
            (TARGET_LOCAL, "Just me"),
        ]

    def test_local_devices_listed_after_escape_hatches(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.devices = {
            "d-alienware": {"label": "Chrome on alienware", "kind": "browser"},
            "d-mbp-deck": {
                "label": "muxplex-deck",
                "display_name": "Stream Deck (macbook)",
                "kind": "deck",
            },
        }
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        options = ctx._target_options()
        assert options[:2] == [(TARGET_SHARED, "Shared"), (TARGET_LOCAL, "Just me")]
        assert ("device:d-alienware", "Chrome on alienware") in options
        # display_name overrides label when both are present.
        assert ("device:d-mbp-deck", "Stream Deck (macbook)") in options

    def test_this_deck_never_offers_itself_as_a_target(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.devices = {
            "d-self": {"label": "this very deck"},
            "d-other": {"label": "some other device"},
        }
        ctx = _make_runtime(deck, client, device_id="d-self")
        ctx.refresh()

        options = ctx._target_options()
        values = [value for value, _label in options]
        assert "device:d-self" not in values
        assert "device:d-other" in values

    def test_no_federated_entries_only_this_servers_own_registry(self) -> None:
        """`_local_devices` only ever reads `raw["devices"]` -- there is no

        mechanism here to reach a peer's registry (Step 6, out of scope).
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.devices = {"d-only-local": {"label": "local device"}}
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        options = ctx._target_options()
        values = {value for value, _label in options}
        assert values == {TARGET_SHARED, TARGET_LOCAL, "device:d-only-local"}


# ---------------------------------------------------------------------------
# target_picker action wiring: dial-scroll/key selection through _ActiveRuntime
# ---------------------------------------------------------------------------


class TestTargetPickerActionDispatch:
    def test_action_opens_and_closes_target_picker(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        ctx.handle_key(0)
        assert ctx.picker.mode == PickerMode.TARGET

        ctx.handle_key(0)  # press again -- closes
        assert ctx.picker.mode == PickerMode.NONE

    def test_selecting_shared_updates_selection_and_next_heartbeat(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        ctx.handle_key(0)  # open picker: [Shared, Just me]
        ctx.handle_key(0)  # slot 0 -> "Shared" (re-affirm, key 0 owns the picker
        # toggle too -- but while OPEN, key.0's binding is irrelevant; the
        # picker owns every key. Selecting slot 0 selects the FIRST option.)

        assert ctx.picker.mode == PickerMode.NONE
        assert ctx.target_selection == TARGET_SHARED
        assert client.heartbeat_calls[-1]["sync_group"] == "global"

    def test_selecting_a_local_device_pairs_and_stays_sticky(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.devices = {"d-alienware": {"label": "Stream Deck (alienware)"}}
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        ctx.handle_key(0)  # open: [Shared, Just me, Stream Deck (alienware)]
        ctx.handle_key(2)  # slot 2 -> the device option

        assert ctx.target_selection == "device:d-alienware"
        assert client.heartbeat_calls[-1]["sync_group"] == "device:d-alienware"

        # Sticky: resent on every subsequent poll tick, not just once.
        ctx.refresh()
        ctx.refresh()
        assert client.heartbeat_calls[-1]["sync_group"] == "device:d-alienware"
        assert client.heartbeat_calls[-2]["sync_group"] == "device:d-alienware"

    def test_empty_slot_selection_exits_picker_without_changing_selection(
        self,
    ) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        ctx.handle_key(0)  # open: only 2 options exist
        ctx.handle_key(5)  # slot 5 -- past the end

        assert ctx.picker.mode == PickerMode.NONE
        assert ctx.target_selection is None  # untouched

    def test_selecting_just_me_sends_no_sync_group(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        ctx.handle_key(0)  # open
        ctx.handle_key(1)  # slot 1 -> "Just me"

        assert ctx.target_selection == TARGET_LOCAL
        assert client.heartbeat_calls[-1]["sync_group"] is None


# ---------------------------------------------------------------------------
# Axis 1 (control target) vs Axis 2 (view_pin/display pinning) independence
# ---------------------------------------------------------------------------


class TestLocalOnlyIsASeparateAxisFromViewPin:
    def test_just_me_freezes_active_session_ignoring_server_updates(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.active_session = "session-00"
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.active_session == "session-00"

        ctx.target_selection = TARGET_LOCAL
        client.active_session = "session-04"  # e.g. someone switches via the PWA
        ctx.refresh()

        assert ctx.active_session == "session-00"  # frozen, never adopted

    def test_just_me_does_not_affect_view_pin_which_stays_none(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        ctx.target_selection = TARGET_LOCAL
        client.active_view = "focus"
        ctx.refresh()

        # view_pin (Axis 2) is untouched by target_selection (Axis 1) --
        # active_view still adopts the server's value normally.
        assert ctx.active_view == "focus"

    def test_view_pin_alone_does_not_freeze_active_session(self) -> None:
        """The inverse check: Step 0's view_pin (Axis 2) must NOT, on its

        own, freeze active_session (Axis 1) -- that would be exactly the
        conflation this step's design review explicitly rejected.
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.active_session = "session-00"
        ctx = _ActiveRuntime(
            deck=cast(DeckDevice, deck),
            client=cast(MuxplexClient, client),
            hostname="test-server",
            sort_mode="server",
            view_pin="all",
            device_id="d-self",
        )
        ctx.view_cycler = ViewCycler(debounce_seconds=_TEST_DEBOUNCE_SECONDS)
        ctx.refresh()
        assert ctx.active_session == "session-00"

        client.active_session = "session-04"
        ctx.refresh()

        assert ctx.active_session == "session-04"  # still adopted -- unpinned axis


# ---------------------------------------------------------------------------
# TargetGoneError / TargetNotSelfOwningError -- degrade sticky + visible
# ---------------------------------------------------------------------------


class TestTargetRejectionDegrades:
    def test_target_gone_falls_back_to_shared_and_retries_same_tick(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        ctx.target_selection = "device:d-vanished"
        client.heartbeat_error = TargetGoneError("target device not registered")

        ctx.refresh()  # must not raise

        assert ctx.target_selection == TARGET_SHARED
        assert client.heartbeat_calls[-2]["sync_group"] == "device:d-vanished"
        assert client.heartbeat_calls[-1]["sync_group"] == "global"
        strip = ctx.last_strip
        assert strip is not None
        assert "target unavailable" in strip
        assert "target gone" in strip

    def test_target_not_self_owning_falls_back_to_shared(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        ctx.target_selection = "device:d-follower-of-mine"
        client.heartbeat_error = TargetNotSelfOwningError(
            "device 'd-self' cannot follow another device while "
            "'d-follower-of-mine' is following it"
        )

        ctx.refresh()  # must not raise

        assert ctx.target_selection == TARGET_SHARED
        assert client.heartbeat_calls[-1]["sync_group"] == "global"
        strip = ctx.last_strip
        assert strip is not None
        assert "cannot follow" in strip

    def test_degrade_is_sticky_does_not_auto_retry_the_rejected_target(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        ctx.target_selection = "device:d-vanished"
        client.heartbeat_error = TargetGoneError("gone")
        ctx.refresh()
        assert ctx.target_selection == TARGET_SHARED

        # Next tick: no error injected this time -- must stay on "shared",
        # never silently retry "device:d-vanished" again on its own.
        ctx.refresh()
        assert ctx.target_selection == TARGET_SHARED
        assert client.heartbeat_calls[-1]["sync_group"] == "global"

    def test_rejection_via_picker_commit_also_degrades_cleanly(self) -> None:
        """The same degrade path fires whether the rejection happens on a

        background poll tick or synchronously inside a picker selection's
        own commit -- `_commit_target` calls `refresh()` directly.
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.devices = {"d-vanished": {"label": "will reject"}}
        ctx = _make_runtime(deck, client, controls={"key.0": "target_picker"})
        ctx.refresh()

        client.heartbeat_error = TargetGoneError("gone")
        ctx.handle_key(0)  # open picker
        ctx.handle_key(
            2
        )  # select the device -> _commit_target -> refresh() raises+degrades

        assert ctx.target_selection == TARGET_SHARED
        assert client.heartbeat_calls[-1]["sync_group"] == "global"


# ---------------------------------------------------------------------------
# Load-bearing regression: untouched target_selection perturbs nothing.
# ---------------------------------------------------------------------------


class TestStep5ByteIdenticalWhenTargetPickerUnused:
    def test_heartbeat_still_omits_sync_group_by_default(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.target_selection is None
        assert client.heartbeat_calls[0]["sync_group"] is None

    def test_strip_gains_only_the_new_trailing_segment(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(3), SETTINGS)
        client.active_session = "session-00"
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        assert ctx.last_strip is not None
        assert ctx.last_strip.startswith(
            "all \u00b7 test-server \u00b7 3 sessions \u00b7 ACTIVE: session-00"
        )
        assert ctx.last_strip.endswith("> shared")

    def test_session_key_press_still_connects_correct_session(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        ctx.handle_key(3)

        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-03"]
        assert ctx.active_session == "session-03"
