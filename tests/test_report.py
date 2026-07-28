"""`muxplex_deck.report` -- the shared VERDICT/STATE/ACTION terminal renderer.

Pure functions, no I/O, no hardware, no service manager -- every test here
constructs `Check`/`Group`/`Readout`/`Action` objects directly and asserts on
the resulting text. Two reference renders from the layout-architect review
are reproduced verbatim (`TestReferenceRenders`) to prove the column ladder,
band framing, and wrapping are implemented exactly as specified; the rest of
the file exercises each mechanic (gutter law, verdict action-counting,
dependency ordering, `--all` superset property, wrapping) in isolation.
"""

from __future__ import annotations

from muxplex_deck import report

# ---------------------------------------------------------------------------
# Reference renders -- byte-identical reproduction of the spec's examples.
# ---------------------------------------------------------------------------


class TestReferenceRenders:
    def test_doctor_fresh_install(self) -> None:
        items: list[report.Item] = [
            report.Check(
                "environment",
                report.FINE,
                "python 3.13.12, muxplex-deck 0.6.1 (pypi, current)",
            ),
            report.Check("config", report.ACT, "not created"),
            report.Check(
                "device",
                report.FINE,
                "Stream Deck Original -- 15 keys (3x5), HID opens",
            ),
            report.Check("server", report.BLOCKED, "waiting on config"),
            report.Check("service", report.BLOCKED, "waiting on config"),
        ]
        collapsed = report.collapsed_checks(items)
        verdict = report.verdict_readiness(
            report.count_actions([c.glyph for c in collapsed])
        )
        state_lines = report.render_items(items, show_all=False, utf8=False)
        action = report.Action(
            decision=report.Decision(
                commands=["muxplex-deck init"],
                prose=(
                    "Creates the config, fetches and fingerprints the server CA, stores the "
                    "federation key, and offers to install the service."
                ),
            )
        )
        action_lines = report.render_action(action)
        text = report.render(verdict, state_lines, action_lines)

        expected = (
            "\n"
            "Not ready -- 1 thing to do.\n"
            "\n"
            "  +  environment   python 3.13.12, muxplex-deck 0.6.1 (pypi, current)\n"
            "  !  config        not created\n"
            "  +  device        Stream Deck Original -- 15 keys (3x5), HID opens\n"
            "  -  server        waiting on config\n"
            "  -  service       waiting on config\n"
            "\n"
            "Do this:\n"
            "\n"
            "    muxplex-deck init\n"
            "\n"
            "  Creates the config, fetches and fingerprints the server CA, stores the\n"
            "  federation key, and offers to install the service.\n"
            "\n"
        )
        assert text == expected

    def test_status_healthy_zero_glyphs(self) -> None:
        readouts = [
            report.Readout("session", "muplex-stream-deck"),
            report.Readout("view", "Focus (page 1)"),
            report.Readout("device", "Stream Deck Original -- 15 keys (3x5)"),
            report.Readout("server", "https://spark-1:8088"),
            report.Readout("pid", "1054028 (updated 2s ago)"),
        ]
        state_lines = report.render_readouts(readouts)
        text = report.render("Running.", state_lines, None)

        expected = (
            "\n"
            "Running.\n"
            "\n"
            "     session       muplex-stream-deck\n"
            "     view          Focus (page 1)\n"
            "     device        Stream Deck Original -- 15 keys (3x5)\n"
            "     server        https://spark-1:8088\n"
            "     pid           1054028 (updated 2s ago)\n"
            "\n"
        )
        assert text == expected

    def test_multiline_remediation_out_of_state_into_action(self) -> None:
        """State stays one line per item even though the fix spans 2 machines."""
        items: list[report.Item] = [
            report.Check(
                "environment",
                report.FINE,
                "python 3.13.12, muxplex-deck 0.6.1 (pypi, current)",
            ),
            report.Check("config", report.FINE, "~/.config/muxplex-deck/config.json"),
            report.Check(
                "device",
                report.ACT,
                "plugged into Windows (busid 1-4), not shared with WSL",
            ),
            report.Check("hid", report.BLOCKED, "waiting on device"),
            report.Check("server", report.FINE, "https://spark-1:8088 (reachable)"),
            report.Check("service", report.BLOCKED, "waiting on device"),
        ]
        state_lines = report.render_items(items, show_all=False, utf8=False)
        # Exactly one line per item -- no matter how elaborate the fix is.
        assert len(state_lines) == len(items)
        assert (
            state_lines[2]
            == "  !  device        plugged into Windows (busid 1-4), not shared with WSL"
        )
        assert state_lines[3] == "  -  hid           waiting on device"


# ---------------------------------------------------------------------------
# Column ladder
# ---------------------------------------------------------------------------


class TestColumnLadder:
    def test_check_line_value_starts_at_column_19(self) -> None:
        line = report.format_check_line(report.FINE, "server", "reachable", utf8=False)
        assert line[19] == "r"  # "reachable" starts here
        assert line[:19] == "  +  server        "

    def test_continuation_hangs_at_column_19(self) -> None:
        line = report.format_check_line(
            report.ACT, "x", "line one\nline two", utf8=False
        )
        lines = line.split("\n")
        assert lines[1] == " " * 19 + "line two"

    def test_readout_line_gutter_is_empty(self) -> None:
        line = report.format_readout_line("pid", "1234")
        assert line[:5] == "     "
        assert line[19] == "1"

    def test_group_label_is_column_zero_lowercase(self) -> None:
        assert report.group_label("Device") == "device"

    def test_subject_padded_to_14(self) -> None:
        line = report.format_check_line(report.FINE, "hid", "ok", utf8=False)
        # "  +  " (5) + "hid" + 11 spaces (14 total) = 19 before value.
        assert line[5:19] == "hid".ljust(14)


# ---------------------------------------------------------------------------
# Gutter law: gutter is empty iff there's nothing to do.
# ---------------------------------------------------------------------------


class TestGutterLaw:
    def test_all_fine_uses_readout_form_no_glyphs(self) -> None:
        readouts = [report.Readout("session", "work")]
        lines = report.render_readouts(readouts)
        assert all(line.startswith("     ") for line in lines)
        assert "+" not in lines[0]

    def test_any_ink_means_work_exists(self) -> None:
        items: list[report.Item] = [
            report.Check("a", report.FINE, "fine"),
            report.Check("b", report.ACT, "broken"),
        ]
        lines = report.render_items(items, show_all=False, utf8=False)
        assert lines[0].startswith("  +  ")
        assert lines[1].startswith("  !  ")


# ---------------------------------------------------------------------------
# Verdict counts ACTIONS, not problems.
# ---------------------------------------------------------------------------


class TestVerdictActionCounting:
    def test_zero_actions_is_ready(self) -> None:
        assert report.verdict_readiness(0) == "Ready."

    def test_one_action_is_singular(self) -> None:
        assert report.verdict_readiness(1) == "Not ready -- 1 thing to do."

    def test_multiple_actions_is_plural(self) -> None:
        assert report.verdict_readiness(3) == "Not ready -- 3 things to do."

    def test_note_is_appended_before_the_final_period(self) -> None:
        assert (
            report.verdict_readiness(1, note="and step 1 needs Windows")
            == "Not ready -- 1 thing to do, and step 1 needs Windows."
        )

    def test_verdict_counts_rows_not_underlying_problems(self) -> None:
        """8 warnings collapsed to 1 actionable row -> verdict says 1, not 8."""
        group = report.Group(
            "environment",
            members=[
                report.Check("python", report.ACT, "too old"),
                report.Check("install", report.ACT, "also broken"),
            ],
        )
        collapsed = report.collapsed_checks([group])
        count = report.count_actions([c.glyph for c in collapsed])
        assert count == 1
        assert report.verdict_readiness(count) == "Not ready -- 1 thing to do."


# ---------------------------------------------------------------------------
# Dependency ordering (never severity) -- render_items preserves input order.
# ---------------------------------------------------------------------------


class TestDependencyOrdering:
    def test_render_items_preserves_caller_supplied_order(self) -> None:
        items: list[report.Item] = [
            report.Check("environment", report.FINE, "ok"),
            report.Check("config", report.ACT, "not created"),
            report.Check("device", report.FINE, "ok"),
            report.Check("server", report.BLOCKED, "waiting on config"),
            report.Check("service", report.BLOCKED, "waiting on config"),
        ]
        lines = report.render_items(items, show_all=False, utf8=False)
        # Each subject appears in the line at its input position (dependency
        # order is caller-owned; the renderer must not reorder for severity).
        for item, line in zip(items, lines, strict=True):
            assert item.subject in line


# ---------------------------------------------------------------------------
# `--all` is a strict superset -- same verdict/action, only STATE expands.
# ---------------------------------------------------------------------------


class TestAllSuperset:
    def test_group_rollup_glyph_matches_worst_member(self) -> None:
        group = report.Group(
            "device",
            members=[
                report.Check("detected", report.ACT, "plugged into Windows"),
                report.Check("hid", report.BLOCKED, "waiting on device"),
            ],
        )
        rolled = group.rollup()
        assert rolled.glyph == report.ACT
        assert rolled.value == "plugged into Windows"

    def test_default_mode_shows_one_line_per_group(self) -> None:
        group = report.Group(
            "config",
            members=[
                report.Check("file", report.FINE, "exists"),
                report.Check("key", report.FINE, "ok"),
                report.Check("ca", report.FINE, "ok"),
            ],
        )
        lines = report.render_items([group], show_all=False, utf8=False)
        assert len(lines) == 1

    def test_all_mode_expands_every_member_under_a_label(self) -> None:
        group = report.Group(
            "config",
            members=[
                report.Check("file", report.FINE, "exists"),
                report.Check("key", report.FINE, "ok"),
                report.Check("ca", report.FINE, "ok"),
            ],
        )
        lines = report.render_items([group], show_all=True, utf8=False)
        assert lines[0] == "config"
        assert len(lines) == 1 + len(group.members)

    def test_verdict_and_action_identical_between_modes(self) -> None:
        group = report.Group(
            "config",
            members=[
                report.Check("file", report.ACT, "not created"),
                report.Check("key", report.FINE, "ok"),
            ],
        )
        collapsed = report.collapsed_checks([group])
        count = report.count_actions([c.glyph for c in collapsed])
        verdict_default = report.verdict_readiness(count)
        verdict_all = report.verdict_readiness(
            count
        )  # computed from the same collapsed list
        assert verdict_default == verdict_all == "Not ready -- 1 thing to do."

    def test_all_mode_never_loses_information_present_in_default(self) -> None:
        """Superset property: every fact visible by default is still present under --all."""
        group = report.Group(
            "environment",
            members=[
                report.Check("python", report.FINE, "3.13.12"),
                report.Check("install", report.ACT, "update available"),
            ],
        )
        default_lines = report.render_items([group], show_all=False, utf8=False)
        all_lines = report.render_items([group], show_all=True, utf8=False)
        default_text = "\n".join(default_lines)
        all_text = "\n".join(all_lines)
        # The default rollup shows the worst member's value; that exact text
        # must still appear somewhere in the --all expansion.
        assert "update available" in default_text
        assert "update available" in all_text


# ---------------------------------------------------------------------------
# Wrapping at 78 columns
# ---------------------------------------------------------------------------


class TestWrapping:
    def test_long_value_wraps_at_78_and_hangs_at_19(self) -> None:
        long_value = (
            "this is a deliberately long value line that should wrap because it "
            "exceeds the seventy eight column budget by a comfortable margin indeed"
        )
        line = report.format_check_line(report.FINE, "x", long_value, utf8=False)
        for physical_line in line.split("\n"):
            assert len(physical_line) <= 78
        lines = line.split("\n")
        assert len(lines) > 1
        assert lines[1].startswith(" " * 19)

    def test_short_lines_are_never_reflowed(self) -> None:
        """A line that already fits is left byte-for-byte alone (bullet text survives)."""
        value = "line one\n    - bulleted continuation\n      further nested text"
        line = report.format_check_line(report.ACT, "x", value, utf8=False)
        lines = line.split("\n")
        assert lines[1] == " " * 19 + "    - bulleted continuation"
        assert lines[2] == " " * 19 + "      further nested text"

    def test_above_80_columns_nothing_stretches(self) -> None:
        line_78 = report.format_check_line(
            report.FINE, "server", "value", utf8=False, width=78
        )
        line_120 = report.format_check_line(
            report.FINE, "server", "value", utf8=False, width=120
        )
        assert line_78 == line_120

    def test_below_60_columns_drops_subject_padding_to_one_space(self) -> None:
        line = report.format_check_line(
            report.FINE, "server", "value", utf8=False, width=50
        )
        assert line == "  +  server value"


# ---------------------------------------------------------------------------
# UTF-8 glyph substitution
# ---------------------------------------------------------------------------


class TestUtf8GlyphSubstitution:
    def test_utf8_capable_reads_lang(self) -> None:
        assert report.utf8_capable({"LANG": "en_US.UTF-8"}) is True
        assert report.utf8_capable({"LANG": "C"}) is False
        assert report.utf8_capable({}) is False

    def test_lc_all_takes_precedence(self) -> None:
        assert report.utf8_capable({"LC_ALL": "en_US.UTF-8", "LANG": "C"}) is True

    def test_fine_glyph_becomes_checkmark_only_under_utf8(self) -> None:
        ascii_line = report.format_check_line(report.FINE, "x", "v", utf8=False)
        utf8_line = report.format_check_line(report.FINE, "x", "v", utf8=True)
        assert ascii_line[2] == "+"
        assert utf8_line[2] == "\u2713"

    def test_act_and_blocked_glyphs_never_change_under_utf8(self) -> None:
        act_ascii = report.format_check_line(report.ACT, "x", "v", utf8=False)
        act_utf8 = report.format_check_line(report.ACT, "x", "v", utf8=True)
        assert act_ascii == act_utf8
        blocked_ascii = report.format_check_line(report.BLOCKED, "x", "v", utf8=False)
        blocked_utf8 = report.format_check_line(report.BLOCKED, "x", "v", utf8=True)
        assert blocked_ascii == blocked_utf8


# ---------------------------------------------------------------------------
# ACTION band: single decision vs multi-step, overflow note.
# ---------------------------------------------------------------------------


class TestActionBand:
    def test_single_decision_command_indented_plus_4_from_do_this(self) -> None:
        action = report.Action(decision=report.Decision(commands=["muxplex-deck init"]))
        lines = report.render_action(action)
        assert lines[0] == "Do this:"
        command_line = next(line_ for line_ in lines if "muxplex-deck init" in line_)
        assert command_line == " " * 4 + "muxplex-deck init"

    def test_prose_is_at_column_2(self) -> None:
        action = report.Action(
            decision=report.Decision(commands=["cmd"], prose="Explanatory text.")
        )
        lines = report.render_action(action)
        prose_line = next(line_ for line_ in lines if "Explanatory" in line_)
        assert prose_line == "  Explanatory text."

    def test_multi_step_numbered_and_commands_indented_from_body(self) -> None:
        action = report.Action(
            steps=[
                report.Step(
                    number=1,
                    intro="On Windows, in an Administrator PowerShell:",
                    commands=["usbipd.exe bind --busid 1-4"],
                ),
                report.Step(
                    number=2,
                    intro="Back here, no admin needed:",
                    commands=["muxplex-deck wsl attach"],
                ),
            ]
        )
        lines = report.render_action(action)
        assert lines[0] == "Do this:"
        step1_line = next(line_ for line_ in lines if line_.strip().startswith("1."))
        body_column = step1_line.index("1.") + len("1. ")
        command_line = next(line_ for line_ in lines if "usbipd.exe" in line_)
        assert command_line.index("usbipd.exe") == body_column + 4

    def test_overflow_note_when_multiple_independent_actions(self) -> None:
        action = report.Action(
            decision=report.Decision(commands=["cmd"]),
            overflow_note="2 more after this -- rerun doctor.",
        )
        lines = report.render_action(action)
        assert "2 more after this -- rerun doctor." in "\n".join(lines)


# ---------------------------------------------------------------------------
# extract_run_command helper
# ---------------------------------------------------------------------------


class TestExtractRunCommand:
    def test_extracts_embedded_run_suggestion(self) -> None:
        message = "Service: not installed -- run: muxplex-deck service install"
        assert report.extract_run_command(message) == "muxplex-deck service install"

    def test_returns_none_when_absent(self) -> None:
        assert report.extract_run_command("all good, nothing to run") is None
