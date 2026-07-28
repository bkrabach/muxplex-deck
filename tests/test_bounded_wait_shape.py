"""Generic regression net: every "wait for X to settle" poll loop in

`service.py` must have a BOUNDED path to a terminal state -- it must give
up and return, not spin forever, if the condition it's waiting for never
becomes true.

This is the shared shape behind incident 3 ("`restart` reported 'may still
be starting' while the task was actually stopped" -- `/Run` fired before
Task Scheduler's own bookkeeping caught up, and the original code had no
poll-until-settled step at all) and the restart-race incident referenced
throughout AGENTS.md (`_wait_for_fresh_status` exists specifically so
`restart` never claims success before a NEW process's status write is
actually observed). Both were fixed by giving the wait a `deadline`
computed once from a `timeout`, polling in a loop, and returning False
(never raising, never spinning) once the deadline passes.

Rather than re-testing each of today's four wait-loops one at a time (they
already have integration-level coverage via their callers in
`test_cli_service.py`/`test_service_windows.py`), this module protects the
SHAPE generically: any function in `service.py` whose name contains
"wait_for" is discovered by naming convention (not a hardcoded list) and
checked for the bounded-poll shape. A future wait-loop that copies the
`while True: ... time.sleep(...)` pattern but forgets the deadline check --
i.e. reintroduces an unbounded "in progress forever" state -- fails this
test immediately.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable

from muxplex_deck import service as service_mod

_WAIT_FUNCTION_NAME_PATTERN = re.compile(r"wait_for", re.IGNORECASE)


def _wait_loop_functions() -> dict[str, Callable[..., object]]:
    """Every callable in `service.py` whose name matches the wait_for* convention."""
    found: dict[str, Callable[..., object]] = {}
    for name in dir(service_mod):
        if _WAIT_FUNCTION_NAME_PATTERN.search(name):
            obj = getattr(service_mod, name)
            if callable(obj):
                found[name] = obj
    return found


def _has_bounded_poll_shape(source: str) -> bool:
    """Structural check: does this poll loop have a deadline it actually

    checks and gives up on? Looks for the three load-bearing pieces of
    the pattern every wait-loop in this module uses: a `deadline`
    computed from `time.monotonic()`, a comparison against it inside the
    loop, and a `time.sleep(...)` between polls (proving it's a poll loop
    at all, not a single check).
    """
    has_deadline = bool(re.search(r"deadline\s*=\s*time\.monotonic\(\)", source))
    has_deadline_check = bool(re.search(r"time\.monotonic\(\)\s*>=\s*deadline", source))
    has_bounded_return = bool(
        re.search(r"time\.monotonic\(\)\s*>=\s*deadline\s*:\s*\n\s*return", source)
    )
    has_sleep = "time.sleep(" in source
    return has_deadline and has_deadline_check and has_bounded_return and has_sleep


class TestEveryWaitLoopIsBounded:
    def test_discovery_finds_the_known_wait_loops(self) -> None:
        """Guards the guard: if the naming convention discovery breaks

        (e.g. every wait function gets renamed away from "wait_for"), this
        fails loudly instead of the whole net silently checking zero
        functions.
        """
        found = _wait_loop_functions()
        assert {
            "_wait_for_fresh_status",
            "_win_wait_for_fresh_status",
            "_wait_for_launchd_unload",
            "_win_wait_for_task_stopped",
        } <= set(found)

    def test_every_discovered_wait_loop_has_a_bounded_exit(self) -> None:
        found = _wait_loop_functions()
        unbounded = [
            name
            for name, fn in found.items()
            if not _has_bounded_poll_shape(inspect.getsource(fn))
        ]
        assert not unbounded, (
            "These poll loops in service.py have no verifiable bounded "
            f"exit (deadline + timeout check + sleep): {unbounded}. A wait "
            "loop with no deadline check can report 'in progress' forever "
            "with no path to a terminal state -- the exact restart-race "
            "bug class this net guards against."
        )

    def test_every_discovered_wait_loop_accepts_a_timeout_parameter(self) -> None:
        """Every wait loop's bound must be caller-configurable (this is

        what lets tests -- and callers with different urgency needs --
        shrink the timeout instead of being stuck with a hardcoded wait).
        """
        found = _wait_loop_functions()
        missing_timeout_param = [
            name
            for name, fn in found.items()
            if "timeout" not in inspect.signature(fn).parameters
        ]
        assert not missing_timeout_param


class TestBoundedPollShapeDetectorCorrectness:
    """Unit-tests `_has_bounded_poll_shape()` against fabricated examples --

    proves the detector actually discriminates bounded-vs-unbounded rather
    than the four real functions merely happening to pass today.
    """

    def test_accepts_the_real_shape(self) -> None:
        source = """
def _wait_for_something(timeout=None):
    if timeout is None:
        timeout = 5.0
    deadline = time.monotonic() + timeout
    while True:
        if condition_met():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
"""
        assert _has_bounded_poll_shape(source)

    def test_rejects_a_loop_with_no_deadline_at_all(self) -> None:
        """The exact regression this net exists to catch: a copy-pasted

        poll loop that checks a condition and sleeps, forever, with no
        way out if the condition never becomes true.
        """
        source = """
def _wait_for_something_forever(timeout=None):
    while True:
        if condition_met():
            return True
        time.sleep(0.2)
"""
        assert not _has_bounded_poll_shape(source)

    def test_rejects_a_deadline_that_is_computed_but_never_checked(self) -> None:
        """A deadline variable that exists but is never compared against

        is just as unbounded as no deadline at all -- dead code masquerading
        as a bound.
        """
        source = """
def _wait_for_something(timeout=None):
    deadline = time.monotonic() + timeout
    while True:
        if condition_met():
            return True
        time.sleep(0.2)
"""
        assert not _has_bounded_poll_shape(source)
