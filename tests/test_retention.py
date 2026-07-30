"""R5 retention: turns >30d pruned, sessions kept (<1yr), daily rollups never
deleted, idempotent second pass.

Rows are inserted directly into the store rather than via ingest.ingest(),
because ingest's own end-of-run pass calls store.prune() with the REAL wall
clock -- which happens to sit inside this corpus's synthetic date range, so
routing through ingest would prune before the test ever gets to inspect the
pre-prune state. Testing store.prune() in isolation is the store-level
retention contract this module owns.
"""

from __future__ import annotations

import datetime as _dt

from ccmetrics import store

NOW = _dt.datetime(2026, 7, 30, 12, 0, 0)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _insert_turn(conn, sid: str, project: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO turns(session_id,project,ts,model,cw5m,cw1h,cread,raw_in,raw_out,"
        "out_bytes,sidechain,version,msg_id) VALUES(?,?,?,?,0,0,100,0,0,0,0,?,?)",
        (sid, project, ts, "claude-haiku-4-5", "1.0", f"{sid}-msg"),
    )
    conn.commit()


def _insert_session(conn, sid: str, project: str, started: str, ended: str) -> None:
    store.upsert_session(
        conn,
        {
            "id": sid, "project": project, "started": started, "ended": ended,
            "turns": 1, "cw5m": 0, "cw1h": 0, "cread": 100, "out_bytes": 0,
            "models": "claude-haiku-4-5", "compactions": 0, "precompact_tokens": 0,
            "sidechain_turns": 0, "hook_errors": 0, "prevented": 0,
        },
    )
    conn.commit()


def _insert_daily(conn, project: str, date: str) -> None:
    store.upsert_daily(
        conn,
        {
            "project": project, "date": date, "floor_usd": 0.0001,
            "cw5m": 0, "cw1h": 0, "cread": 100, "out_bytes": 0, "turns": 1,
        },
    )
    conn.commit()


def test_prune_turns_older_than_30d_sessions_kept_rollups_kept(conn, cc_env):
    project = "-Users-retention-proj"
    old_ts = _iso(NOW - _dt.timedelta(days=40))
    recent_ts = _iso(NOW - _dt.timedelta(days=5))

    _insert_turn(conn, "old-sess", project, old_ts)
    _insert_turn(conn, "recent-sess", project, recent_ts)
    _insert_session(conn, "old-sess", project, old_ts, old_ts)
    _insert_session(conn, "recent-sess", project, recent_ts, recent_ts)
    _insert_daily(conn, project, old_ts[:10])
    _insert_daily(conn, project, recent_ts[:10])

    assert conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"] == 2
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2
    daily_before = conn.execute("SELECT COUNT(*) c FROM daily").fetchone()["c"]
    assert daily_before == 2

    result = store.prune(conn, _iso(NOW), path=cc_env["db_path"])
    assert result["turns_deleted"] == 1

    remaining = [r["session_id"] for r in conn.execute("SELECT session_id FROM turns")]
    assert remaining == ["recent-sess"]

    # sessions are kept: the old session ended only 40 days ago, session_days=365
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 2

    # daily rollups are NEVER pruned, regardless of age
    daily_after = conn.execute("SELECT COUNT(*) c FROM daily").fetchone()["c"]
    assert daily_after == daily_before


def test_prune_sessions_older_than_1yr_removed(conn, cc_env):
    project = "-Users-retention-proj2"
    ancient_ts = _iso(NOW - _dt.timedelta(days=400))
    _insert_turn(conn, "ancient-sess", project, ancient_ts)
    _insert_session(conn, "ancient-sess", project, ancient_ts, ancient_ts)
    _insert_daily(conn, project, ancient_ts[:10])
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 1

    store.prune(conn, _iso(NOW), path=cc_env["db_path"])
    # the session ended > session_days(365) ago: removed
    assert conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"] == 0
    # its rollup row still stands
    assert conn.execute("SELECT COUNT(*) c FROM daily").fetchone()["c"] == 1


def test_prune_idempotent(conn, cc_env):
    project = "-Users-retention-proj3"
    old_ts = _iso(NOW - _dt.timedelta(days=40))
    _insert_turn(conn, "old-sess", project, old_ts)
    _insert_session(conn, "old-sess", project, old_ts, old_ts)
    _insert_daily(conn, project, old_ts[:10])

    first = store.prune(conn, _iso(NOW), path=cc_env["db_path"])
    assert first["turns_deleted"] == 1

    second = store.prune(conn, _iso(NOW), path=cc_env["db_path"])
    assert second["turns_deleted"] == 0
    assert second["sessions_deleted"] == 0
    assert second["tool_calls_deleted"] == 0
