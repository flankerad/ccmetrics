"""First-run auto-wiring of login autostart on the plain console report.

`ccmetrics` / `ccmetrics --project=...` registers its own login services on
its very first console run, announces it in one line, and never touches it
again. `autostart.apply` itself is exercised directly in test_autostart.py
against a tmp_path HOME; here `cli._first_run_autostart` is exercised
through a fake `autostart.apply` so no test ever writes real LaunchAgent or
systemd files, mirroring tests/test_first_run_setup.py and
tests/test_first_run_dash.py in shape.

`cc_env` (tests/conftest.py) sets CCMETRICS_NO_AUTOSTART=1 as a safe default
for every test in the suite, so any test elsewhere that drives `main()` with
a tty never touches autostart. This module exists to exercise that gate
directly, so each test below first undoes the default (delenv) and then
re-applies whichever single gate it means to prove.

The isatty patch is applied inside each test body (not a setup-phase
fixture): capsys swaps in a fresh sys.stdout between the fixture-setup
phase and the test-call phase, so a patch made during setup would be lost."""

from __future__ import annotations

import sys

import pytest

from ccmetrics import autostart, store
from ccmetrics.cli import main


def _make_tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


def _no_default_no_autostart(monkeypatch) -> None:
    monkeypatch.delenv("CCMETRICS_NO_AUTOSTART", raising=False)


@pytest.fixture(autouse=True)
def _no_dash(monkeypatch):
    # This module only exercises the autostart gate; the dashboard/widget
    # first-run launch is covered in tests/test_first_run_dash.py.
    from ccmetrics import cli

    monkeypatch.setattr(cli, "_first_run_dash", lambda *a, **k: None)


@pytest.fixture
def fake_apply(monkeypatch):
    calls = []

    def _fake(*a, **k):
        calls.append((a, k))
        return {"changed": True, "has_tk": True, "message": "wrote stuff"}

    monkeypatch.setattr(autostart, "apply", _fake)
    return calls


def test_non_tty_stays_silent(cc_env, capsys, monkeypatch, fake_apply):
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, False)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "starts its dashboard" not in out
    assert fake_apply == []


def test_json_stays_silent(cc_env, capsys, monkeypatch, fake_apply):
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--json"]) == 0
    out = capsys.readouterr().out
    assert "starts its dashboard" not in out
    assert fake_apply == []


def test_env_var_stays_silent(cc_env, capsys, monkeypatch, fake_apply):
    # undo the fixture default, then prove the env var itself gates it
    _no_default_no_autostart(monkeypatch)
    monkeypatch.setenv("CCMETRICS_NO_AUTOSTART", "1")
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "starts its dashboard" not in out
    assert fake_apply == []


def test_no_autostart_flag_stays_silent(cc_env, capsys, monkeypatch, fake_apply):
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--no-autostart"]) == 0
    out = capsys.readouterr().out
    assert "starts its dashboard" not in out
    assert fake_apply == []


def test_happy_path_registers_once_and_prints(cc_env, capsys, monkeypatch, conn, fake_apply):
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "ccmetrics now starts its dashboard and widget automatically at login." in out
    assert "undo any time: ccmetrics autostart --revert" in out
    assert len(fake_apply) == 1
    first_meta = store.get_meta(conn, "autostart_autowire")
    assert first_meta is not None

    # second run: meta already set -- silent, autostart.apply not called
    # again, and the meta value itself is untouched (not just the call count)
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out2 = capsys.readouterr().out
    assert "starts its dashboard" not in out2
    assert len(fake_apply) == 1
    assert store.get_meta(conn, "autostart_autowire") == first_meta


def test_tk_missing_prints_the_dashboard_only_message(cc_env, capsys, monkeypatch, conn):
    monkeypatch.setattr(
        autostart, "apply", lambda *a, **k: {"changed": True, "has_tk": False, "message": "wrote stuff"}
    )
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert (
        "ccmetrics now starts its dashboard automatically at login "
        "(skipping the widget -- this Python build has no tkinter)." in out
    )
    assert "undo any time: ccmetrics autostart --revert" in out


def test_unchanged_result_stays_silent(cc_env, capsys, monkeypatch, conn):
    """apply() reporting changed=False (already installed) prints nothing,
    same as `_first_run_statusline`'s already-wired branch -- but the meta
    key is still set so this never runs again."""
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, True)
    monkeypatch.setattr(
        autostart, "apply", lambda *a, **k: {"changed": False, "message": "already installed"}
    )
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "starts its dashboard" not in out
    assert store.get_meta(conn, "autostart_autowire") is not None


def test_unsupported_platform_never_burns_the_meta_key(cc_env, capsys, monkeypatch, conn):
    """An unsupported platform must never spend the once-only meta key: a
    future ccmetrics build that adds support gets to try again."""
    _no_default_no_autostart(monkeypatch)
    _make_tty(monkeypatch, True)
    monkeypatch.setattr(autostart, "_platform_kind", lambda: "unsupported")

    def _boom(*a, **k):
        raise AssertionError("apply() must not run when the platform is unsupported")

    monkeypatch.setattr(autostart, "apply", _boom)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "starts its dashboard" not in out
    assert store.get_meta(conn, "autostart_autowire") is None
