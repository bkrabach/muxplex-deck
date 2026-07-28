"""Regression net for incident 1 ("`status` reported a healthy service as

'previous run's data', forever"): `service_main_pid()` on Windows used to
be assumed to return `IRunningTask.EnginePID` as the sidecar's own live
pid. VERIFIED FALSE on real hardware -- `EnginePID` names the Task
Scheduler ENGINE-HOST process, never the task it launched (Microsoft's own
docs, confirmed independently since 2011). Comparing that fabricated value
against the sidecar's self-reported pid in `status.json` could never match,
so a perfectly healthy service was permanently reported as showing a
"previous run's" stale data.

The shared shape this net protects, generalized beyond this one incident:
**any pid used for a liveness/identity comparison must come from the
sidecar's own self-report (or an equally-traceable proxy, like the
Windows baseline-pid-diff -- see `_win_wait_for_fresh_status`'s
docstring), never from a value that merely LOOKS authoritative.**
`service_main_pid()` is the one function in this codebase whose entire
contract is "return the sidecar's own live pid, or None if that can't be
determined honestly" -- so this net has two jobs:

1. Structurally pin that the Windows branch never forwards
   `_win_task_query()`'s `EnginePID` as if it were that authoritative
   value (behavioral coverage for the same fact already exists in
   `test_service_windows.py::TestWindowsPredicates::
   test_main_pid_always_none_even_when_task_running_with_a_pid` -- this
   module adds the structural companion so a future refactor can't
   quietly reintroduce `return info.pid` and still pass a test that only
   happens to monkeypatch a matching scenario).
2. Structurally pin that the one call site that actually performs the
   pid-freshness comparison (`cli.status()`) sources its "live pid" from
   `service_main_pid()` and nothing else.
"""

from __future__ import annotations

import inspect
import re

from muxplex_deck import cli
from muxplex_deck import service as service_mod


def _windows_branch_of(source: str) -> str:
    """Isolate the `if _is_windows(): ...` branch body from a function's

    source text, up to the next same-indent (`\\n    `) statement. Assumes
    the windows branch is followed by another 4-space-indented statement
    in the same function (true for `service_main_pid()`'s shape today: the
    Windows branch is immediately followed by the POSIX `try:` fallback).
    """
    assert "if _is_windows():" in source, "no _is_windows() branch found"
    remainder = source.split("if _is_windows():", 1)[1]
    # Cut at the next line that starts at the function-body indent level
    # (4 spaces followed by a non-space character) -- that's the next
    # statement after this branch ends.
    match = re.search(r"\n {4}\S", remainder)
    return remainder[: match.start()] if match else remainder


class TestServiceMainPidWindowsBranchNeverFabricatesAPid:
    def test_windows_branch_never_returns_win_task_query_pid(self) -> None:
        source = inspect.getsource(service_mod.service_main_pid)
        windows_branch = _windows_branch_of(source)

        assert "info.pid" not in windows_branch, (
            "service_main_pid()'s Windows branch must never forward "
            "_win_task_query()'s pid -- that field is EnginePID, the Task "
            "Scheduler engine-HOST process, never the sidecar's own pid "
            "(VERIFIED on real hardware; see the function's own "
            "docstring). Doing so would fabricate an authoritative-"
            "looking value that is actively wrong, reintroducing the "
            "exact incident this net guards against."
        )
        assert "_win_task_query" not in windows_branch, (
            "the Windows branch should not even query task state to "
            "answer 'what is the sidecar's own pid' -- there is no COM "
            "property that provides it, so the honest answer is 'cannot "
            "determine' (None), not a value derived from a query that "
            "cannot answer the question."
        )
        assert re.search(r"return\s+None\b", windows_branch), (
            "the Windows branch must return None unconditionally"
        )


class TestStatusPidComparisonSourcesFromServiceMainPid:
    def test_status_reads_its_live_pid_from_service_main_pid(self) -> None:
        """`cli.status()`'s "is this snapshot current" check must source

        its live pid via a direct `service_main_pid()` call -- not a
        differently-named local, not a re-derivation from `_win_task_query`
        or any other lower-level primitive. Behavioral coverage for the
        comparison's OUTCOME already exists in
        `test_cli_status.py::TestStatusPidFreshnessGuard`; this pins the
        PROVENANCE so a future edit can't swap in a different, less
        trustworthy pid source while still passing those outcome tests
        (which only fake `service_main_pid` itself, so they can't detect
        a call site regression on their own).
        """
        source = inspect.getsource(cli.status)
        assert re.search(r"current_pid\s*=\s*service_main_pid\(\)", source), (
            "cli.status() must assign current_pid = service_main_pid() "
            "directly -- this is the sidecar's own self-report (or the "
            "honest 'cannot determine' None), the only source status() is "
            "allowed to trust for a liveness comparison."
        )


class TestWindowsBranchExtractionHelperCorrectness:
    """Unit-tests `_windows_branch_of()` against a fabricated regression --

    proves the detector would actually catch a future `return info.pid`
    reintroduction, not just that today's real source happens to pass.
    """

    def test_flags_a_fabricated_regression_that_forwards_the_pid(self) -> None:
        fake_source = """
def service_main_pid():
    if _is_darwin():
        return 1

    if _is_windows():
        info = _win_task_query()
        return info.pid

    try:
        return 2
"""
        branch = _windows_branch_of(fake_source)
        assert "info.pid" in branch  # the detector's assertion would fail on this

    def test_accepts_the_real_honest_shape(self) -> None:
        fake_source = """
def service_main_pid():
    if _is_darwin():
        return 1

    if _is_windows():
        return None

    try:
        return 2
"""
        branch = _windows_branch_of(fake_source)
        assert "info.pid" not in branch
        assert re.search(r"return\s+None\b", branch)
