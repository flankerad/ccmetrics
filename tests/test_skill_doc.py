"""Guards `.claude/skills/ccmetrics/SKILL.md` and `README.md` against
flag/subcommand drift.

Twice now a row in SKILL.md named a flag that does not exist on the
subcommand it named, so the agent following the doc ran a command that died
with an argparse usage error (exit 2). This extracts every `ccmetrics ...`
command mentioned in each doc and parses each one against the real CLI
parser -- a doc edit that invents a flag or subcommand fails here before it
ships.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from ccmetrics.cli import _build_parser

ROOT = Path(__file__).resolve().parent.parent
SKILL_MD = ROOT / ".claude" / "skills" / "ccmetrics" / "SKILL.md"
README_MD = ROOT / "README.md"

# The doc's one deliberate negative example: top-level flags used after a
# subcommand, which argparse rejects. Named explicitly rather than detected
# by scanning for words like "fails" -- that heuristic breaks the moment the
# doc's wording changes.
FAILING_COMMAND = "ccmetrics detectors --all-leaks"


def _backtick_spans(path: Path) -> list[str]:
    """Inline `code` spans in `path`, fenced ```code blocks``` stripped first.

    A fenced block's opening and closing ``` are each three backticks run
    together, so the single-backtick regex below pairs the first two as an
    empty match and then greedily pairs the third with the next *unrelated*
    single backtick later in the doc -- swallowing an entire multi-line
    example (README.md's sample dashboard output) into one bogus "command".
    Dropping fenced blocks up front avoids that false pairing.
    """
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.DOTALL)
    return re.findall(r"`([^`]+)`", text)


def _positive_commands(path: Path, *, exclude: frozenset[str] = frozenset()) -> list[str]:
    """Every `ccmetrics ...` command backticked in `path`, minus `exclude`.

    A bare flag span (e.g. `--all-leaks --evidence`, `--no-setup`) is doc
    shorthand for a ccmetrics command in context -- prefix it back on.
    Anything else (a subcommand word on its own, a slash command like
    `/compact`, a path like `~/.claude/settings.json`, a JSON field like
    `usage.input_tokens`, a bare env var) is prose, not a command, and is
    skipped.
    """
    commands = []
    for span in _backtick_spans(path):
        tokens = span.split()
        if not tokens:
            continue
        if tokens[0] == "ccmetrics":
            command = span
        elif span.startswith("--"):
            command = f"ccmetrics {span}"
        else:
            continue
        if command in exclude:
            continue
        commands.append(command)
    return commands


def _skill_commands() -> list[str]:
    return _positive_commands(SKILL_MD, exclude=frozenset({FAILING_COMMAND}))


def _readme_commands() -> list[str]:
    return _positive_commands(README_MD)


def _subcommands_named(path: Path) -> list[str]:
    """Subcommand names appearing right after `ccmetrics` in an extracted command."""
    names = []
    for span in _backtick_spans(path):
        tokens = span.split()
        if len(tokens) > 1 and tokens[0] == "ccmetrics" and not tokens[1].startswith("-"):
            names.append(tokens[1])
    return names


def test_negative_example_is_present_and_still_fails():
    assert FAILING_COMMAND in _backtick_spans(SKILL_MD)
    tokens = shlex.split(FAILING_COMMAND)
    with pytest.raises(SystemExit):
        _build_parser().parse_args(tokens[1:])


def test_positive_commands_set_is_non_empty_and_excludes_the_negative_example():
    commands = _skill_commands()
    assert commands
    assert FAILING_COMMAND not in commands


def test_positive_commands_include_the_leak_flags():
    commands = _skill_commands()
    assert "ccmetrics --all-leaks" in commands
    assert "ccmetrics --all-leaks --evidence" in commands


@pytest.mark.parametrize("command", _skill_commands())
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
    names = _subcommands_named(SKILL_MD)
    assert names
    for name in names:
        assert name in sub_action.choices


def test_readme_commands_set_is_non_empty_and_includes_the_undo_commands():
    # Deleting the setup/autostart --apply/--revert table from README should
    # fail this before it ships undocumented, un-guarded undo instructions.
    commands = _readme_commands()
    assert commands
    assert "ccmetrics setup --apply" in commands
    assert "ccmetrics autostart --revert" in commands


@pytest.mark.parametrize("command", _readme_commands())
def test_readme_documented_command_parses_cleanly(command):
    tokens = shlex.split(command)
    # Never execute -- dash/widget/live/otel are long-running and bare
    # `ccmetrics` writes settings on first run. parse_args only validates.
    _build_parser().parse_args(tokens[1:])
