"""Guards against a crashing `--help`.

argparse `%`-formats every help string at render time (`format_help()` /
`--help`), so a bare `%` anywhere in a help string raises `ValueError` the
moment help is actually rendered. 109 tests stayed green while
`ccmetrics --help` crashed for every user, because nothing in the suite ever
called `format_help()` or invoked `--help`. These tests render help for the
top-level parser and every subcommand (discovered dynamically, so new
subcommands are covered automatically) and check that `--help` exits cleanly
with code 0.
"""

from __future__ import annotations

import argparse

import pytest

from ccmetrics.cli import _build_parser, main


def _subparsers_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._subparsers._group_actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers action found on parser")


def _sub_names() -> list[str]:
    return sorted(_subparsers_action(_build_parser()).choices)


def test_top_level_help_renders_without_raising():
    # A bare '%' in any top-level help string raises ValueError here -- that
    # is the exact regression phase 01 fixed.
    _build_parser().format_help()


@pytest.mark.parametrize("name", _sub_names())
def test_subcommand_help_renders_without_raising(name):
    parser = _build_parser()
    sub = _subparsers_action(parser).choices[name]
    sub.format_help()


def test_top_level_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


@pytest.mark.parametrize("name", _sub_names())
def test_subcommand_help_exits_zero(name, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([name, "--help"])
    assert excinfo.value.code == 0


def test_statusline_help_literal_percent_survives_in_top_level_help():
    # The statusline subparser's `help=` text ("...plan %%...") is rendered
    # by the *top-level* parser's format_help() (in the subcommand summary
    # list), where argparse %-expands it. A correct double-escape ('%%')
    # collapses to one literal '%'; a bare '%' would have raised already in
    # test_top_level_help_renders_without_raising above.
    text = _build_parser().format_help()
    assert "plan %" in text
    assert "plan %%" not in text
