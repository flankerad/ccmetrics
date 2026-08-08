"""Plain `ccmetrics` on a machine that never installed/ran Claude Code: no
~/.claude/projects directory exists. The empty-report is confusing and the
first-run wiring would otherwise mkdir ~/.claude just to write into it, so
this path short-circuits before ingest, before the status line auto-wire,
and before the first-run dashboard -- nothing may be written into ~/.claude.
"""

from __future__ import annotations

import json
import sys

import pytest

from ccmetrics import plan, store
from ccmetrics.cli import main


@pytest.fixture
def no_claude(tmp_path, monkeypatch):
    """Points every ~/.claude-shaped path at tmp locations that do not
    exist, so the missing-projects-dir branch is exercised without ever
    touching the real ~/.claude."""
    projects_dir = tmp_path / "claude_projects"  # deliberately never created
    settings = tmp_path / "dotclaude" / "settings.json"
    monkeypatch.setenv("CCMETRICS_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setenv("CCMETRICS_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("CCMETRICS_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    monkeypatch.delenv("CCMETRICS_CLAUDE_DIR", raising=False)
    monkeypatch.setattr(plan, "default_settings_path", lambda: settings)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    return {"projects_dir": projects_dir, "settings": settings}


def test_prose_path_when_no_sessions_dir(no_claude, capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "ccmetrics found no Claude Code sessions on this machine." in out
    assert str(no_claude["projects_dir"]) in out
    assert "Install Claude Code, use it once, then run ccmetrics again." in out
    assert "$0.00" not in out
    assert "0 turns" not in out


def test_json_form_when_no_sessions_dir(no_claude, capsys):
    assert main(["--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload == {"error": "no_claude_code", "path": str(no_claude["projects_dir"])}


def test_writes_nothing_into_claude_dir(no_claude, capsys, monkeypatch):
    from ccmetrics import cli

    calls = []
    monkeypatch.setattr(cli, "_first_run_dash", lambda *a, **k: calls.append("dash"))
    assert main([]) == 0
    capsys.readouterr()
    assert not no_claude["settings"].exists()
    assert not no_claude["settings"].parent.exists()
    assert calls == []
    conn = store.connect(store.db_path())
    try:
        assert store.get_meta(conn, "statusline_autowire") is None
        assert store.get_meta(conn, "first_run_dash") is None
    finally:
        conn.close()
