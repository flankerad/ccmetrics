"""Shared fixtures. Every test runs against a tmp_path projects dir + tmp_path
DB via env overrides -- never ~/.claude, never ~/.local/share/ccmetrics.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ccmetrics import live, store  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_live_state():
    """live.py keeps process-local tail state in a module dict; never let one
    test's tailed file bleed into the next."""
    live.reset()
    yield
    live.reset()


@pytest.fixture
def cc_env(tmp_path, monkeypatch):
    projects_dir = tmp_path / "claude_projects"
    projects_dir.mkdir()
    db_path = tmp_path / "state.db"
    monkeypatch.setenv("CCMETRICS_PROJECTS_DIR", str(projects_dir))
    monkeypatch.setenv("CCMETRICS_DB", str(db_path))
    monkeypatch.delenv("CCMETRICS_CLAUDE_DIR", raising=False)
    # Point the /usage-cache reader at a file that does not exist by default --
    # no test may ever read the real ~/.claude.json (PLAN-dash-v2 7).
    monkeypatch.setenv("CCMETRICS_CLAUDE_CONFIG", str(tmp_path / "claude.json"))
    # Safe default for every test: never let the first-run dashboard launch
    # bind a real socket or open real Tk just because a test drives `main()`
    # with a tty. Tests that specifically exercise that gate delenv this.
    monkeypatch.setenv("CCMETRICS_NO_DASH", "1")
    # Same reasoning, for autostart: never let a test that drives `main()`
    # with a tty write real LaunchAgent plists or systemd units into the
    # developer's actual home directory. Tests that specifically exercise
    # that gate delenv this.
    monkeypatch.setenv("CCMETRICS_NO_AUTOSTART", "1")
    return {"projects_dir": projects_dir, "db_path": db_path}


@pytest.fixture
def conn(cc_env):
    c = store.connect(cc_env["db_path"])
    yield c
    c.close()
