"""D60: the plain `ccmetrics` summary path calls `widget.restart_if_outdated`
so a user who never opens the widget still gets an outdated dash restarted.
A failure anywhere in that check (no dash, a restart that itself raises)
must never stop the summary the command exists to print.
"""

from __future__ import annotations

import json
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


# --- D62: outdated dash that couldn't self-heal (no usable pid file) -------
#
# These drive the real `widget.restart_if_outdated` (not a stub) through
# `main`, faking only the network fetch and pid-file lookup it uses, so the
# CLI notice is proven against the actual D59/D60 gap rather than a
# hand-picked return value.


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_notice_printed_once_when_outdated_and_no_pid_file(cc_env, capsys, monkeypatch):
    # Pre-D59 dash: answers, but is outdated and wrote no pid file at all.
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                         lambda *a, **k: _FakeResponse({"server_version": "0.1.0"}))
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: None)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert out.count("ccmetrics dash is running an older version") == 1
    assert "ccmetrics dash" in out and "↻" in out


def test_no_notice_when_outdated_but_pid_file_usable(cc_env, capsys, monkeypatch):
    # Outdated, but a usable pid file lets the restart actually run.
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                         lambda *a, **k: _FakeResponse({"server_version": "0.1.0"}))
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: (111, 7433))
    monkeypatch.setattr(widget, "_pid_alive", lambda pid: True)
    calls = []
    monkeypatch.setattr(widget.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(widget, "_wait_port_silent", lambda port: calls.append("wait"))
    monkeypatch.setattr(widget.subprocess, "Popen", lambda *a, **k: calls.append("spawn"))
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "older version" not in out
    assert ("kill", 111, widget.signal.SIGTERM) in calls
    assert "spawn" in calls


def test_no_notice_when_server_current(cc_env, capsys, monkeypatch):
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                         lambda *a, **k: _FakeResponse({"server_version": widget.__version__}))
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "older version" not in out


def test_no_notice_when_no_server_answering(cc_env, capsys, monkeypatch):
    def _raise(*a, **k):
        raise widget.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(widget.urllib.request, "urlopen", _raise)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "older version" not in out
