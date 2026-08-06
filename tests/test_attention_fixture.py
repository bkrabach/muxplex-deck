"""Cross-implementation agreement: `apply_attention_sort()` vs. the shared fixture.

`tests/fixtures/attention_sort_cases.json` is a byte-for-byte duplicate of
muxplex's `tests/fixtures/attention_sort_cases.json` -- the two live in
separate git repos with independently versioned releases, so the contract
is enforced by keeping identical fixture content in both places rather than
a single shared file. The SAME cases are also consumed by:
- muxplex's `tests/test_attention_order_fixture.py` (`_attention_order()`)
- muxplex's `frontend/tests/test_attention_fixture.mjs` (`sortByAttention()`)

docs/API_SEMANTICS.md (in muxplex) requires all three implementations to
move together. This fixture is the mechanism that turns a drift in any one
of them into a test FAILURE instead of a silent divergence discovered later
in production -- see muxplex's AGENTS.md for the v0.38.1 incident that
prompted (then, on further diagnosis, reversed) the tier this fixture now
pins the absence of.
"""

from __future__ import annotations

import json
from pathlib import Path

from muxplex_client import Bell, Session

from muxplex_deck.attention import apply_attention_sort

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "attention_sort_cases.json"


def _load_cases() -> list[dict]:
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return data["cases"]


def _to_session(raw: dict) -> Session:
    bell_raw = raw["bell"]
    bell = Bell(
        last_fired_at=bell_raw["last_fired_at"],
        seen_at=bell_raw["seen_at"],
        unseen_count=bell_raw["unseen_count"],
    )
    return Session(
        name=raw["name"],
        snapshot="",
        bell=bell,
        last_activity_at=raw.get("last_activity_at"),
    )


def test_apply_attention_sort_matches_fixture_for_every_case() -> None:
    cases = _load_cases()
    assert cases, "fixture must not be empty -- an empty fixture would pass vacuously"

    for case in cases:
        # apply_attention_sort() no longer takes an active-session argument
        # at all -- active_session in the fixture exists only to prove it
        # has no effect on ordering.
        sessions = [_to_session(raw) for raw in case["sessions"]]
        ordered = apply_attention_sort(sessions)
        names = [s.name for s in ordered]
        assert names == case["expected_order"], (
            f"case {case['name']!r}: apply_attention_sort() produced {names}, "
            f"expected {case['expected_order']} (see {FIXTURE_PATH})"
        )
