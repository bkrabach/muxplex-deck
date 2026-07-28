"""Generic regression net: every argparse flag/positional this CLI defines

must be READ somewhere in `cli.main()`'s dispatch, or it can be silently
dropped exactly like `--log-file` was (see `test_cli_dispatch.py`'s
`TestLogFileReachesRun` class and WINDOWS_NATIVE_SPEC.md section 1.5's
follow-up for the real incident: argparse correctly parsed `--log-file` in
every case, but `main()`'s final dispatch branch never read `args.log_file`
back out, so the value was thrown away regardless of where on the command
line it appeared).

That incident was fixed by hand, one flag at a time -- this module is the
net that makes the *next* one impossible to miss, generically: it does not
hardcode "log_file" or any other flag name. Instead it:

1. Walks `cli._build_parser()`'s own actions (recursing into every
   subparser) to get the ground-truth set of `dest` names this CLI
   defines, right now, whatever they are.
2. Scans `cli.main()`'s dispatch source (parser construction was moved out
   into `_build_parser()` specifically so this source is ONLY dispatch
   code, not `add_argument()` calls that would trivially "contain" the
   dest string without ever reading it) for a reference to each dest via
   `args.<dest>` or `getattr(args, "<dest>", ...)`.
3. Fails loudly, naming the exact dest(s), if any flag defined by the
   parser is never referenced by the dispatch code.

A flag added to `_build_parser()` in the future with no matching read in
`main()`'s dispatch will fail this test immediately -- no one needs to
remember to write a bespoke regression test for it, the way `--log-file`
needed one after the fact.
"""

from __future__ import annotations

import argparse
import inspect
import re
from collections.abc import Iterator

from muxplex_deck import cli

# Dests that are handled by argparse itself (help) or exist purely as
# subparser-selector plumbing whose *value* IS read (via `args.command`,
# `args.config_command`, etc. -- those dests are still included and
# checked normally). Only argparse's own built-in `--help` is exempt: it
# never reaches user dispatch code by design (argparse intercepts it).
_EXEMPT_DESTS = {"help", argparse.SUPPRESS}


def _iter_actions(parser: argparse.ArgumentParser) -> Iterator[argparse.Action]:
    """Yield every action defined on `parser`, recursing into subparsers."""
    for action in parser._actions:
        yield action
        if isinstance(action, argparse._SubParsersAction):
            for sub_parser in action.choices.values():
                yield from _iter_actions(sub_parser)


def _dest_is_referenced(dest: str, source: str) -> bool:
    """Does `source` read this dest back via `args.<dest>` or `getattr(args, "<dest>"`?"""
    patterns = (
        rf"args\.{re.escape(dest)}\b",
        rf"getattr\(\s*args\s*,\s*[\"']{re.escape(dest)}[\"']",
    )
    return any(re.search(pattern, source) for pattern in patterns)


class TestEveryCliFlagIsReadInDispatch:
    def test_every_defined_dest_is_referenced_in_main_dispatch(self) -> None:
        parser = cli._build_parser()
        dests = {
            action.dest
            for action in _iter_actions(parser)
            if action.dest not in _EXEMPT_DESTS
        }
        assert dests, "sanity check: the parser should define at least one flag"

        dispatch_source = inspect.getsource(cli.main)
        missing = sorted(
            d for d in dests if not _dest_is_referenced(d, dispatch_source)
        )

        assert not missing, (
            "These argparse dests are defined by _build_parser() but never "
            f"read in cli.main()'s dispatch: {missing}. This is the exact "
            "bug class that silently dropped --log-file (see this module's "
            "docstring): argparse parses the flag correctly, but nothing "
            "ever reads args.<dest> back out, so the value is thrown away "
            "no matter what the user typed."
        )

    def test_build_parser_itself_contains_no_dispatch_logic(self) -> None:
        """Guards the separation this test relies on: if dispatch code

        (reading `args.*`) crept back into `_build_parser()`, the coverage
        check above would start passing vacuously (the dest string would
        appear next to its own `add_argument()` call, or worse, dispatch
        would silently duplicate between the two functions). `main()`
        should be the ONLY place that reads `args` after parsing.
        """
        build_parser_source = inspect.getsource(cli._build_parser)
        assert "args." not in build_parser_source
        assert "parse_args" not in build_parser_source


class TestDetectorCorrectness:
    """Unit-tests `_dest_is_referenced()` itself against fabricated

    examples -- proves the detector actually discriminates read-vs-not-read
    rather than the real dispatch source merely happening to pass today.
    """

    def test_flags_a_dest_that_is_never_read(self) -> None:
        fake_dispatch = (
            "def main():\n    args = parser.parse_args()\n    print(args.command)\n"
        )
        assert not _dest_is_referenced("totally_new_flag", fake_dispatch)

    def test_accepts_a_dest_read_via_dot_access(self) -> None:
        fake_dispatch = "def main():\n    x = args.totally_new_flag\n"
        assert _dest_is_referenced("totally_new_flag", fake_dispatch)

    def test_accepts_a_dest_read_via_getattr(self) -> None:
        fake_dispatch = 'value = getattr(args, "totally_new_flag", None)'
        assert _dest_is_referenced("totally_new_flag", fake_dispatch)

    def test_does_not_false_positive_on_a_prefix_match(self) -> None:
        """`args.log_file_extra` must not satisfy a check for `log_file`."""
        fake_dispatch = "x = args.log_file_extra"
        assert not _dest_is_referenced("log_file", fake_dispatch)
