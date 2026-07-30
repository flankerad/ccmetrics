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
    return {"projects_dir": projects_dir, "db_path": db_path}


@pytest.fixture
def conn(cc_env):
    c = store.connect(cc_env["db_path"])
    yield c
    c.close()
