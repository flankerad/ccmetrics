"""First-run auto-wiring of the status line on the plain console report.

`ccmetrics` / `ccmetrics --project=...` wires its own statusLine into
settings.json on its very first console run, announces it in one line, and
never touches it again. Every path is exercised through
`plan.default_settings_path` patched to a tmp file so no test ever touches
the real ~/.claude.

The isatty patch is applied inside each test body (not a setup-phase
fixture): capsys swaps in a fresh sys.stdout between the fixture-setup
phase and the test-call phase, so a patch made during setup would be lost."""

from __future__ import annotations

import sys

import pytest

from ccmetrics import plan, store
from ccmetrics.cli import main


def _make_tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(plan, "default_settings_path", lambda: settings)
    return settings


def test_non_tty_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, False)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_json_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--json"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_env_var_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, True)
    monkeypatch.setenv("CCMETRICS_NO_SETUP", "1")
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_no_setup_flag_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--no-setup"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_happy_path_wires_once_and_prints(cc_env, capsys, monkeypatch, conn, _settings):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert (
        "ccmetrics wired its status line into ~/.claude/settings.json — "
        "your plan usage (5h and weekly %) now shows there." in out
    )
    assert "undo any time: ccmetrics setup --revert" in out
    assert _settings.exists()
    assert "statusline" in _settings.read_text()
    assert store.get_meta(conn, "statusline_autowire") is not None

    # second run: meta already set -- silent, apply not called again
    written_at = _settings.read_text()
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out2 = capsys.readouterr().out
    assert "wired its status line" not in out2
    assert _settings.read_text() == written_at


def test_setup_error_path_prints_one_line_and_marks_meta(cc_env, capsys, monkeypatch, conn, _settings):
    _make_tty(monkeypatch, True)
    _settings.write_text("{not json")
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "ccmetrics left your status line alone:" in out
    assert "wired its status line" not in out
    assert store.get_meta(conn, "statusline_autowire") is not None
