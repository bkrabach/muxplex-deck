"""Deck-side "Step 1" (walking skeleton) of

`muxplex/docs/plans/2026-08-16-deck-control-target-design.md` §8.3/§8.4/§10:
identity + labels + kind, everyone stays global.

Covers three things, deliberately kept in one file since they're one
feature:

1. `identity.load_device_id` -- mints a UUID v4 once, persists it next to
   whichever `config.json` is in effect, and reuses it across a simulated
   restart (a fresh call against the same path).
2. `_ActiveRuntime` passes `device_id=` on every group-touching client call
   (`state()`/`connect()`/`set_active_view()`) and fires `heartbeat()` on
   the existing poll tick, under the existing `client_lock`, with the
   right `label`/`kind` -- `config.name` overrides the hostname-derived
   default.
3. The load-bearing regression: with device_id/heartbeat now wired in,
   this deck's actual session-filtering/connect/view behavior is
   unchanged -- same shape of proof as `test_view_pin.py`'s
   `TestNoPinIsByteIdenticalRegression`, just for identity instead of
   view pinning.

Uses the same `FakeDeck`/`FakeClient` fakes as test_runtime_modes.py (no
hardware, no server, no real threads left dangling).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from muxplex_client import MuxplexClient
from test_runtime_modes import SETTINGS, FakeClient, FakeDeck, _make_sessions

from muxplex_deck import identity as identity_mod
from muxplex_deck import main as main_mod
from muxplex_deck.config import DEFAULT_CONFIG, Config
from muxplex_deck.device import DeckDevice
from muxplex_deck.interaction import ViewCycler
from muxplex_deck.main import CLIENT_KIND, _ActiveRuntime

_TEST_DEBOUNCE_SECONDS = 0.05
_WAIT_SECONDS = 5.0


# ---------------------------------------------------------------------------
# identity.py -- mint once, persist, reuse across a simulated restart
# ---------------------------------------------------------------------------


class TestLoadDeviceId:
    def test_mints_a_valid_uuid_v4_on_first_call(self, tmp_path: Path) -> None:
        import uuid

        config_path = str(tmp_path / "config.json")
        device_id = identity_mod.load_device_id(config_path)
        # Raises ValueError if not a valid UUID; version 4 specifically.
        parsed = uuid.UUID(device_id)
        assert parsed.version == 4

    def test_persists_next_to_the_config_file(self, tmp_path: Path) -> None:
        config_path = str(tmp_path / "config.json")
        identity_mod.load_device_id(config_path)
        identity_path = tmp_path / "identity.json"
        assert identity_path.exists()
        data = json.loads(identity_path.read_text(encoding="utf-8"))
        assert isinstance(data["device_id"], str) and data["device_id"]

    def test_survives_a_simulated_restart_same_id_returned(
        self, tmp_path: Path
    ) -> None:
        """The load-bearing case: the deck must not churn its identity

        every launch (design doc assumption 2 / risk 3) -- a SECOND,
        independent call against the same config path (standing in for
        the sidecar process restarting) returns the exact same id, not a
        freshly minted one.
        """
        config_path = str(tmp_path / "config.json")
        first = identity_mod.load_device_id(config_path)
        second = identity_mod.load_device_id(config_path)
        assert first == second

    def test_two_different_config_paths_get_independent_identities(
        self, tmp_path: Path
    ) -> None:
        one = identity_mod.load_device_id(str(tmp_path / "a" / "config.json"))
        two = identity_mod.load_device_id(str(tmp_path / "b" / "config.json"))
        assert one != two

    def test_missing_identity_file_mints_fresh(self, tmp_path: Path) -> None:
        config_path = str(tmp_path / "config.json")
        assert not (tmp_path / "identity.json").exists()
        device_id = identity_mod.load_device_id(config_path)
        assert device_id  # non-empty
        assert (tmp_path / "identity.json").exists()

    def test_corrupt_identity_file_regenerates_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        config_path = str(tmp_path / "config.json")
        identity_path = tmp_path / "identity.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text("not json{{{", encoding="utf-8")
        device_id = identity_mod.load_device_id(config_path)
        assert device_id  # regenerated, not raised
        data = json.loads(identity_path.read_text(encoding="utf-8"))
        assert data["device_id"] == device_id

    def test_missing_device_id_key_regenerates(self, tmp_path: Path) -> None:
        config_path = str(tmp_path / "config.json")
        identity_path = tmp_path / "identity.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(json.dumps({"nonsense": True}), encoding="utf-8")
        device_id = identity_mod.load_device_id(config_path)
        assert device_id

    def test_empty_string_device_id_regenerates(self, tmp_path: Path) -> None:
        """A blank id on disk is treated as absent, not a valid (empty) identity."""
        config_path = str(tmp_path / "config.json")
        identity_path = tmp_path / "identity.json"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        identity_path.write_text(json.dumps({"device_id": ""}), encoding="utf-8")
        device_id = identity_mod.load_device_id(config_path)
        assert device_id != ""

    def test_default_identity_path_follows_config_path_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No explicit config_path -- falls back to `MUXPLEX_DECK_CONFIG`

        (or the default path), exactly like `config._resolve_config_path`
        itself -- this is what makes the identity file automatically
        test-isolated by the existing Rail 1 (`_isolate_config_default_path`
        in conftest.py) with no separate rail needed.
        """
        fake_default = tmp_path / "default-config.json"
        monkeypatch.setenv("MUXPLEX_DECK_CONFIG", str(fake_default))
        resolved = identity_mod.default_identity_path(None)
        assert resolved == tmp_path / "identity.json"


# ---------------------------------------------------------------------------
# _ActiveRuntime -- device_id passthrough, heartbeat, and label resolution
# ---------------------------------------------------------------------------


def _make_full_deck() -> FakeDeck:
    return FakeDeck(
        key_count=8, key_layout=(2, 4), key_size=(120, 120), dial_count=4, is_touch=True
    )


def _make_runtime(
    deck: FakeDeck,
    client: FakeClient,
    *,
    device_id: str = "d-test-0001",
    name: str = "",
) -> _ActiveRuntime:
    ctx = _ActiveRuntime(
        deck=cast(DeckDevice, deck),
        client=cast(MuxplexClient, client),
        hostname="test-server",
        sort_mode="server",
        device_id=device_id,
        name=name,
    )
    ctx.view_cycler = ViewCycler(debounce_seconds=_TEST_DEBOUNCE_SECONDS)
    return ctx


class TestDeviceIdPassthrough:
    """`device_id=` must be passed on every group-touching client call."""

    def test_state_call_carries_device_id(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-abc123")
        ctx.refresh()
        assert client.state_device_ids == ["d-abc123"]

    def test_connect_call_carries_device_id(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-abc123")
        ctx.refresh()
        ctx.handle_key(0)
        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connect_device_ids == ["d-abc123"]

    def test_set_active_view_call_carries_device_id(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-abc123")
        ctx.refresh()
        ctx.handle_dial_turn(0, "view_cycle", 1)
        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patch_device_ids == ["d-abc123"]

    def test_repeated_refreshes_keep_sending_the_same_device_id(self) -> None:
        """The id must not drift/regenerate across poll ticks within one session."""
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-stable")
        ctx.refresh()
        ctx.refresh()
        ctx.refresh()
        assert client.state_device_ids == ["d-stable", "d-stable", "d-stable"]


class TestHeartbeatOnPollTick:
    """`heartbeat()` fires on the existing poll tick, under `client_lock`."""

    def test_refresh_fires_exactly_one_heartbeat(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-hb")
        ctx.refresh()
        assert client.heartbeat_event.wait(_WAIT_SECONDS)
        assert len(client.heartbeat_calls) == 1

    def test_heartbeat_carries_device_id_and_kind(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-hb")
        ctx.refresh()
        call = client.heartbeat_calls[0]
        assert call["device_id"] == "d-hb"
        assert call["kind"] == CLIENT_KIND == "deck"

    def test_heartbeat_never_sends_a_sync_group(self) -> None:
        """Step 1 stays in `global` -- never sets `sync_group` to anything

        but its omitted/None default (ADR §10: "everyone stays global").
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-hb")
        ctx.refresh()
        assert client.heartbeat_calls[0]["sync_group"] is None

    def test_heartbeat_fires_before_the_device_id_scoped_state_call(self) -> None:
        """Ordering matters: an unregistered device_id 404s at the HTTP

        boundary (ADR §2.1), so `heartbeat()` must register/refresh this
        deck's identity before `state(device_id=...)` ever asks the
        server to resolve anything by that id -- on every tick, not just
        the first.
        """
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        call_order: list[str] = []

        original_heartbeat = client.heartbeat
        original_state = client.state

        def recording_heartbeat(**kwargs: object) -> None:
            call_order.append("heartbeat")
            original_heartbeat(**kwargs)  # type: ignore[arg-type]

        def recording_state(**kwargs: object):
            call_order.append("state")
            return original_state(**kwargs)  # type: ignore[arg-type]

        client.heartbeat = recording_heartbeat  # type: ignore[method-assign]
        client.state = recording_state  # type: ignore[method-assign]

        ctx = _make_runtime(deck, client, device_id="d-order")
        ctx.refresh()

        assert call_order == ["heartbeat", "state"]

    def test_multiple_refreshes_fire_multiple_heartbeats(self) -> None:
        """Heartbeat rides the EXISTING poll tick -- once per `refresh()`, no throttling."""
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, device_id="d-hb")
        ctx.refresh()
        ctx.refresh()
        ctx.refresh()
        assert len(client.heartbeat_calls) == 3


class TestDeviceLabelResolution:
    """`config.name` is the default label; empty means "derive from hostname"."""

    def test_configured_name_is_sent_as_label(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, name="alienware-deck")
        ctx.refresh()
        assert client.heartbeat_calls[0]["label"] == "alienware-deck"

    def test_empty_name_falls_back_to_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(main_mod.socket, "gethostname", lambda: "fallback-host")
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, name="")
        ctx.refresh()
        assert client.heartbeat_calls[0]["label"] == "fallback-host"

    def test_configured_name_takes_priority_over_hostname(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            main_mod.socket, "gethostname", lambda: "should-not-be-used"
        )
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, name="my-studio-deck")
        ctx.refresh()
        assert client.heartbeat_calls[0]["label"] == "my-studio-deck"

    def test_apply_reload_picks_up_a_changed_name_without_restart(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client, name="old-name")
        ctx.refresh()

        reloaded = Config(
            server_url="https://example.test:8088",
            federation_key="sekrit",
            ca_file=None,
            poll_interval=2.0,
            sort="server",
            view_pin=None,
            name="new-name",
            controls={},
        )
        ctx.apply_reload(reloaded)
        ctx.refresh()

        assert ctx.name == "new-name"
        assert client.heartbeat_calls[-1]["label"] == "new-name"


class TestActiveRemoteIdRoundTrips:
    """ADR §8.1 #10 -- parsed and stored, never acted on yet (Step 5's job)."""

    def test_defaults_to_none(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.active_remote_id is None

    def test_round_trips_a_non_none_value_without_erroring(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.active_remote_id = "remote-alienware-uuid"
        ctx = _make_runtime(deck, client)
        ctx.refresh()  # must not raise
        assert ctx.active_remote_id == "remote-alienware-uuid"

    def test_clearing_active_remote_id_is_observed_on_next_refresh(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(5), SETTINGS)
        client.active_remote_id = "remote-alienware-uuid"
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.active_remote_id == "remote-alienware-uuid"

        client.active_remote_id = None
        ctx.refresh()
        assert ctx.active_remote_id is None


# ---------------------------------------------------------------------------
# The load-bearing test: identity/heartbeat plumbing perturbs NOTHING else.
#
# Mirrors test_view_pin.py's own `TestNoPinIsByteIdenticalRegression`
# exactly, but for the identity/label/kind wiring instead of view pinning:
# the same session-filtering/connect/view assertions that existed before
# device_id/heartbeat were added, still passing, run through THIS file's
# own `_make_runtime` (which now threads device_id/name through
# `_ActiveRuntime.__init__`).
# ---------------------------------------------------------------------------


class TestByteIdenticalRegression:
    def test_session_key_press_still_connects_correct_session(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        ctx.handle_key(3)

        assert client.connect_event.wait(_WAIT_SECONDS)
        assert client.connected_names == ["session-03"]
        assert ctx.active_session == "session-03"

    def test_view_dial_turn_still_commits_debounced_patch(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        ctx.handle_dial_turn(0, "view_cycle", 1)

        assert client.view_patch_event.wait(_WAIT_SECONDS)
        assert client.view_patches == ["focus"]

    def test_process_still_adopts_server_active_view(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        assert ctx.active_view == "all"

        client.active_view = "focus"
        ctx.refresh()

        assert ctx.active_view == "focus"
        assert {s.name for s in ctx.ordered} == {"session-00", "session-01"}

    def test_page_still_resets_on_server_side_view_change(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()
        ctx.handle_dial_turn(1, "page_cycle", 1)
        assert ctx.pager.page == 2

        client.active_view = "focus"
        ctx.refresh()

        assert ctx.pager.page == 1

    def test_page_dial_still_pages_and_clamps_and_never_connects(self) -> None:
        deck = _make_full_deck()
        client = FakeClient(_make_sessions(20), SETTINGS)
        ctx = _make_runtime(deck, client)
        ctx.refresh()

        ctx.handle_dial_turn(1, "page_cycle", 1)
        assert ctx.pager.page == 2
        ctx.handle_dial_turn(1, "page_cycle", 5)
        assert ctx.pager.page == 3  # clamped at last page
        ctx.handle_dial_turn(1, "page_cycle", -10)
        assert ctx.pager.page == 1  # clamped at first page
        assert client.connected_names == []

    def test_default_config_name_key_present_and_empty(self) -> None:
        """Full accounting alongside `TestNameParsing`: a config produced

        by `config_reset`/a fresh install has `name: ""`, matching this
        module's own `_device_label()` fallback-to-hostname contract.
        """
        assert DEFAULT_CONFIG["name"] == ""
