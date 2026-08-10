"""Guards `.claude/skills/ccmetrics/SKILL.md` against flag/subcommand drift.

Twice now a row in SKILL.md named a flag that does not exist on the
subcommand it named, so the agent following the doc ran a command that died
with an argparse usage error (exit 2). This extracts every `ccmetrics ...`
command mentioned in the doc and parses each one against the real CLI
parser -- a doc edit that invents a flag or subcommand fails here before it
ships.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from ccmetrics.cli import _build_parser

SKILL_MD = Path(__file__).resolve().parent.parent / ".claude" / "skills" / "ccmetrics" / "SKILL.md"

# The doc's one deliberate negative example: top-level flags used after a
# subcommand, which argparse rejects. Named explicitly rather than detected
# by scanning for words like "fails" -- that heuristic breaks the moment the
# doc's wording changes.
FAILING_COMMAND = "ccmetrics detectors --all-leaks"


def _backtick_spans() -> list[str]:
    return re.findall(r"`([^`]+)`", SKILL_MD.read_text())


def _positive_commands() -> list[str]:
    """Every `ccmetrics ...` command in SKILL.md, minus the negative example.

    A bare flag span (e.g. `--all-leaks --evidence`) is doc shorthand for a
    ccmetrics command in context -- prefix it back on. Anything else (a
    subcommand word on its own, a path, an env var) is prose, not a command,
    and is skipped.
    """
    commands = []
    for span in _backtick_spans():
        tokens = span.split()
        if not tokens:
            continue
        if tokens[0] == "ccmetrics":
            command = span
        elif span.startswith("--"):
            command = f"ccmetrics {span}"
        else:
            continue
        if command == FAILING_COMMAND:
            continue
        commands.append(command)
    return commands


def _subcommands_named() -> list[str]:
    """Subcommand names appearing right after `ccmetrics` in an extracted command."""
    names = []
    for span in _backtick_spans():
        tokens = span.split()
        if len(tokens) > 1 and tokens[0] == "ccmetrics" and not tokens[1].startswith("-"):
            names.append(tokens[1])
    return names


def test_negative_example_is_present_and_still_fails():
    assert FAILING_COMMAND in _backtick_spans()
    tokens = shlex.split(FAILING_COMMAND)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(tokens[1:])


def test_positive_commands_set_is_non_empty_and_excludes_the_negative_example():
    commands = _positive_commands()
    assert commands
    assert FAILING_COMMAND not in commands


def test_positive_commands_include_the_leak_flags():
    commands = _positive_commands()
    assert "ccmetrics --all-leaks" in commands
    assert "ccmetrics --all-leaks --evidence" in commands


@pytest.mark.parametrize("command", _positive_commands())
def test_documented_command_parses_cleanly(command):
    tokens = shlex.split(command)
    # Never execute -- dash/widget/live/otel are long-running and bare
    # `ccmetrics` writes settings on first run. parse_args only validates.
    _build_parser().parse_args(tokens[1:])


def test_subcommands_named_in_doc_are_real_subparser_choices():
    parser = _build_parser()
    sub_action = next(
        a for a in parser._subparsers._group_actions if hasattr(a, "choices")
    )
    names = _subcommands_named()
    assert names
    for name in names:
        assert name in sub_action.choices
