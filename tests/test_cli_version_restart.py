"""D60: the plain `ccmetrics` summary path calls `widget.restart_if_outdated`
so a user who never opens the widget still gets an outdated dash restarted.
A failure anywhere in that check (no dash, a restart that itself raises)
must never stop the summary the command exists to print.
"""

from __future__ import annotations

import sys

import pytest

from ccmetrics import widget
from ccmetrics.cli import main


def _make_tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


@pytest.fixture(autouse=True)
def _stay_shut(monkeypatch):
    # This module only exercises the D60 version check; first-run dash/
    # statusline/autostart auto-wire is covered elsewhere.
    monkeypatch.setenv("CCMETRICS_NO_DASH", "1")


def test_summary_calls_restart_if_outdated(cc_env, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "restart_if_outdated", lambda: calls.append(1))
    assert main(["--no-ingest"]) == 0
    assert calls == [1]


def test_summary_still_prints_when_restart_if_outdated_raises(cc_env, capsys, monkeypatch):
    def _boom():
        raise RuntimeError("dash unreachable")

    monkeypatch.setattr(widget, "restart_if_outdated", _boom)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "ccmetrics" in out


def test_json_summary_still_prints_when_restart_if_outdated_raises(cc_env, capsys, monkeypatch):
    def _boom():
        raise RuntimeError("dash unreachable")

    monkeypatch.setattr(widget, "restart_if_outdated", _boom)
    assert main(["--no-ingest", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"summary"' in out
