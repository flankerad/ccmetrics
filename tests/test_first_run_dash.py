"""First-run auto-launch of the dashboard on the plain console report.

`ccmetrics` opens its own dash (and the floating widget, when Tk is
available) on its very first console run, then never again. Every path is
exercised through fakes -- no test ever opens a real socket, a real
browser, a real Tk window, or touches the real ~/.claude.
"""

from __future__ import annotations

import sys

import pytest

from ccmetrics import cli, dash, store, widget
from ccmetrics.cli import main


def _make_tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


class _FakeThread:
    """Runs the target synchronously so tests never race a real thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture(autouse=True)
def _no_statusline(monkeypatch):
    # This module only exercises the dash gate; the status line auto-wire
    # is covered in tests/test_first_run_setup.py.
    monkeypatch.setattr(cli, "_first_run_statusline", lambda *a, **k: None)


@pytest.fixture
def fake_serve(monkeypatch):
    calls = []
    monkeypatch.setattr(dash, "serve", lambda **kw: calls.append(kw) or 0)
    return calls


@pytest.fixture
def fake_widget(monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "run", lambda **kw: calls.append(kw) or 0)
    return calls


@pytest.fixture
def sync_thread(monkeypatch):
    monkeypatch.setattr(cli.threading, "Thread", _FakeThread)


def test_non_tty_stays_shut(cc_env, capsys, monkeypatch, fake_serve):
    _make_tty(monkeypatch, False)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "opening your dashboard" not in out
    assert fake_serve == []


def test_json_stays_shut(cc_env, capsys, monkeypatch, fake_serve):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--json"]) == 0
    out = capsys.readouterr().out
    assert "opening your dashboard" not in out
    assert fake_serve == []


def test_env_var_stays_shut(cc_env, capsys, monkeypatch, fake_serve):
    _make_tty(monkeypatch, True)
    monkeypatch.setenv("CCMETRICS_NO_DASH", "1")
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "opening your dashboard" not in out
    assert fake_serve == []


def test_no_dash_flag_stays_shut(cc_env, capsys, monkeypatch, fake_serve):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--no-dash"]) == 0
    out = capsys.readouterr().out
    assert "opening your dashboard" not in out
    assert fake_serve == []


def test_happy_path_with_tk_starts_dash_and_widget_once(
    cc_env, capsys, monkeypatch, conn, fake_serve, fake_widget, sync_thread
):
    monkeypatch.setitem(sys.modules, "tkinter", sys.modules.get("tkinter") or object())
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "opening your dashboard — press ctrl-c to stop it" in out
    assert len(fake_serve) == 1
    assert fake_serve[0]["port"] == 7433
    assert len(fake_widget) == 1
    assert store.get_meta(conn, "first_run_dash") is not None

    # second run: meta already set -- neither serve nor widget fire again
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out2 = capsys.readouterr().out
    assert "opening your dashboard" not in out2
    assert len(fake_serve) == 1
    assert len(fake_widget) == 1


def test_tk_missing_serves_without_widget(cc_env, capsys, monkeypatch, conn, fake_serve, fake_widget):
    monkeypatch.setitem(sys.modules, "tkinter", None)  # forces ImportError
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "opening your dashboard — press ctrl-c to stop it" in out
    assert "Tk" not in out
    assert len(fake_serve) == 1
    assert fake_widget == []
    assert store.get_meta(conn, "first_run_dash") is not None


def test_ctrl_c_on_serve_exits_quietly(cc_env, capsys, monkeypatch, conn):
    monkeypatch.setitem(sys.modules, "tkinter", None)  # forces ImportError

    def _raise(**kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(dash, "serve", _raise)
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
