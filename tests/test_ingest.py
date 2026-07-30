"""Ingest contract: dedupe, watermark resume, truncation, malformed lines,
unknown record types, half-written trailing line. All synthetic fixtures."""

from __future__ import annotations

import datetime as _dt

from ccmetrics import ingest

from .util import (
    assistant_rec,
    make_project,
    session_path,
    ts_at,
    write_lines,
    write_raw,
)

BASE = _dt.datetime(2026, 7, 20, 12, 0, 0)


def test_dedupe_by_session_and_msg_id(conn, cc_env):
    """Claude Code repeats the same message.id + usage on several lines (one
    per content block). Usage must be counted exactly once."""
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(
        f,
        [
            assistant_rec(
                "sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5",
                cw5m=100, cw1h=0, cread=50, text="first block",
            ),
            assistant_rec(
                "sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5",
                cw5m=100, cw1h=0, cread=50, text="second block",
            ),
        ],
    )
    stats = ingest.ingest(conn, cc_env["projects_dir"])
    assert stats["turns_new"] == 1

    row = conn.execute("SELECT cw5m, cw1h, cread, out_bytes FROM turns").fetchone()
    assert row["cw5m"] == 100
    assert row["cread"] == 50
    # both text blocks contribute their own bytes even though usage counted once
    assert row["out_bytes"] == len(b"first block") + len(b"second block")


def test_dedupe_tool_use_blocks_across_repeated_lines(conn, cc_env):
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(
        f,
        [
            assistant_rec(
                "sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10,
                tool_uses=[{"id": "tu1", "name": "Read", "input": {"file_path": "/a.py"}}],
            ),
            assistant_rec(
                "sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10,
                tool_uses=[{"id": "tu2", "name": "Read", "input": {"file_path": "/b.py"}}],
            ),
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    n_turns = conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
    n_calls = conn.execute("SELECT COUNT(*) c FROM tool_calls").fetchone()["c"]
    assert n_turns == 1
    assert n_calls == 2  # different tool_use_id -> both kept


def test_watermark_resume_no_double_count(conn, cc_env):
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(
        f,
        [
            assistant_rec("sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10),
            assistant_rec("sess1", "msg-2", ts_at(BASE, 5), "claude-haiku-4-5", cread=10),
        ],
    )
    first = ingest.ingest(conn, cc_env["projects_dir"])
    assert first["turns_new"] == 2

    # nothing changed: re-run must not re-read the file at all
    second = ingest.ingest(conn, cc_env["projects_dir"])
    assert second["files_read"] == 0
    assert second["turns_new"] == 0

    # append two more lines, re-ingest: only the new ones count
    write_lines(
        f,
        [
            assistant_rec("sess1", "msg-3", ts_at(BASE, 10), "claude-haiku-4-5", cread=10),
            assistant_rec("sess1", "msg-4", ts_at(BASE, 15), "claude-haiku-4-5", cread=10),
        ],
    )
    third = ingest.ingest(conn, cc_env["projects_dir"])
    assert third["turns_new"] == 2

    total = conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
    assert total == 4


def test_truncation_reset(conn, cc_env):
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(
        f,
        [
            assistant_rec("sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10),
            assistant_rec("sess1", "msg-2", ts_at(BASE, 5), "claude-haiku-4-5", cread=10),
            assistant_rec("sess1", "msg-3", ts_at(BASE, 10), "claude-haiku-4-5", cread=10),
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    assert conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 3

    # simulate a rewritten (shorter) file: truncate then write fresh content
    f.write_bytes(b"")
    write_lines(f, [assistant_rec("sess2", "msg-new", ts_at(BASE, 100), "claude-haiku-4-5", cread=99)])

    stats = ingest.ingest(conn, cc_env["projects_dir"])
    assert stats["turns_new"] == 1
    row = conn.execute(
        "SELECT session_id, cread FROM turns WHERE session_id='sess2'"
    ).fetchone()
    assert row["cread"] == 99


def test_malformed_relevant_line_counted_not_fatal(conn, cc_env):
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(f, [assistant_rec("sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10)])
    # a line that LOOKS relevant (carries "usage") but is not valid JSON
    write_raw(f, b'{"type":"assistant","message":{"usage": this is not json\n')
    write_lines(f, [assistant_rec("sess1", "msg-2", ts_at(BASE, 5), "claude-haiku-4-5", cread=10)])

    stats = ingest.ingest(conn, cc_env["projects_dir"])
    assert stats["parse_failures"] == 1
    assert stats["turns_new"] == 2  # the good lines around it still land


def test_unknown_record_type_ignored(conn, cc_env):
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(
        f,
        [
            # carries "usage" so it passes the byte prefilter, but its type is
            # neither assistant/user/system
            {"type": "weirdtype", "sessionId": "sess1", "timestamp": ts_at(BASE, 0), "usage": {}},
            assistant_rec("sess1", "msg-1", ts_at(BASE, 5), "claude-haiku-4-5", cread=10),
        ],
    )
    stats = ingest.ingest(conn, cc_env["projects_dir"])
    assert stats["unknown_types"].get("weirdtype") == 1
    assert stats["turns_new"] == 1
    assert stats["parse_failures"] == 0


def test_half_written_last_line_held_back(conn, cc_env):
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(f, [assistant_rec("sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10)])
    # a partial line, no trailing newline: session file mid-write
    from .util import dumps

    rec2 = assistant_rec("sess1", "msg-2", ts_at(BASE, 5), "claude-haiku-4-5", cread=20)
    body = dumps(rec2).encode("utf-8")
    write_raw(f, body[: len(body) // 2])  # no trailing \n

    stats = ingest.ingest(conn, cc_env["projects_dir"])
    assert stats["turns_new"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 1

    # complete the line; next ingest must pick it up
    write_raw(f, body[len(body) // 2 :] + b"\n")
    stats2 = ingest.ingest(conn, cc_env["projects_dir"])
    assert stats2["turns_new"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 2
