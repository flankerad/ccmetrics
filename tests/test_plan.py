"""Optional plan-limit feed (`ccmetrics statusline`): extract whitelist +
sanitization, record throttle, dash /api/plan payload + staleness contract,
report PLAN line, and the v3->v4 plan_snapshots migration."""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

from ccmetrics import plan, report, store

BASE = _dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(dt: _dt.datetime) -> int:
    return int(dt.timestamp())


# --- 1. extract: whitelist + sanitize ---------------------------------------


def test_extract_doc_shaped_payload_both_windows_with_pct_and_resets():
    five_hour_reset = BASE + _dt.timedelta(hours=3)
    seven_day_reset = BASE + _dt.timedelta(days=4)
    payload = {
        "session_id": "sess-1",
        "model": {"id": "claude-opus-5", "display_name": "Opus"},
        "cost": {"total_cost_usd": 0.01234},
        "context_window": {"used_percentage": 8},
        "rate_limits": {
            "five_hour": {"used_percentage": 31, "resets_at": _epoch(five_hour_reset)},
            "seven_day": {"used_percentage": 62.4, "resets_at": _epoch(seven_day_reset)},
        },
    }
    out = plan.extract(payload)
    assert out["session_id"] == "sess-1"
    assert out["model"] == "Opus"
    assert out["cost_usd"] == pytest.approx(0.01234)
    assert out["context_pct"] == pytest.approx(8.0)

    windows = out["windows"]
    assert set(windows) == {"five_hour", "seven_day"}
    assert windows["five_hour"]["used_pct"] == pytest.approx(31.0)
    assert windows["five_hour"]["resets_at"] == _iso(five_hour_reset)
    assert windows["seven_day"]["used_pct"] == pytest.approx(62.4)
    assert windows["seven_day"]["resets_at"] == _iso(seven_day_reset)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        "not a dict",
        123,
        [],
        {"rate_limits": None},  # missing/wrong-typed rate_limits
        {"session_id": "s"},  # missing rate_limits entirely
        {"rate_limits": "nope"},
        {"rate_limits": {"five_hour": "not a dict"}},
        {"rate_limits": {"five_hour": {"used_percentage": "99;DROP TABLE turns"}}},
        {"rate_limits": {"five_hour": {"used_percentage": -5}}},
        {"rate_limits": {"five_hour": {"used_percentage": 400}}},
        {"rate_limits": {"five_hour": {"used_percentage": float("nan")}}},
        {"rate_limits": {"five_hour": {"used_percentage": float("inf")}}},
        {"rate_limits": {"five_hour": {"used_percentage": True}}},  # bool, not a number
        {"rate_limits": {"5h!! weird/name": {"used_percentage": 50}}},
        {"rate_limits": {"": {"used_percentage": 50}}},
        {"rate_limits": {"x" * 40: {"used_percentage": 50}}},  # over the 32-char cap
        {"rate_limits": {123: {"used_percentage": 50}}},  # non-string window name
    ],
)
def test_extract_junk_shapes_never_raise_and_store_nothing(payload):
    out = plan.extract(payload)
    assert out["windows"] == {}


def test_extract_bad_resets_at_still_keeps_the_percentage():
    # resets_at is sanitized independently: a hostile/garbage reset time drops
    # only the reset, never the percentage that came with it.
    out = plan.extract(
        {"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": "DROP TABLE"}}}
    )
    assert out["windows"]["five_hour"]["used_pct"] == pytest.approx(50.0)
    assert out["windows"]["five_hour"]["resets_at"] is None


def test_extract_out_of_era_resets_at_dropped():
    out = plan.extract(
        {"rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": 1}}}
    )
    assert out["windows"]["five_hour"]["resets_at"] is None


def test_extract_unrecognised_window_name_kept_verbatim():
    # window names ccmetrics has not documented are still stored, lowercased,
    # under whatever name Claude Code gave them -- never invented, never dropped.
    out = plan.extract(
        {"rate_limits": {"Seven_Day_Opus": {"used_percentage": 10, "resets_at": _epoch(BASE)}}}
    )
    assert out["windows"] == {"seven_day_opus": {"used_pct": 10.0, "resets_at": _iso(BASE)}}


# --- 2. record: throttle -----------------------------------------------------


def _windows_payload():
    return plan.extract(
        {
            "rate_limits": {
                "five_hour": {"used_percentage": 20, "resets_at": _epoch(BASE + _dt.timedelta(hours=2))},
                "seven_day": {"used_percentage": 40, "resets_at": _epoch(BASE + _dt.timedelta(days=3))},
            }
        }
    )


def test_record_within_throttle_window_is_dropped(conn):
    data = _windows_payload()
    t0 = _iso(BASE)
    n1 = plan.record(conn, data, now_iso=t0)
    assert n1 == 2  # one row per window

    t1 = _iso(BASE + _dt.timedelta(seconds=1))  # 1s later, well under 20s
    n2 = plan.record(conn, data, now_iso=t1)
    assert n2 == 0

    total = conn.execute("SELECT COUNT(*) c FROM plan_snapshots").fetchone()["c"]
    assert total == 2  # only the first snapshot set landed


def test_record_past_throttle_window_writes_a_second_set(conn):
    data = _windows_payload()
    t0 = _iso(BASE)
    plan.record(conn, data, now_iso=t0)

    t1 = _iso(BASE + _dt.timedelta(seconds=25))  # > 20s min interval
    n2 = plan.record(conn, data, now_iso=t1)
    assert n2 == 2

    total = conn.execute("SELECT COUNT(*) c FROM plan_snapshots").fetchone()["c"]
    assert total == 4  # two distinct snapshot sets


def test_record_with_no_windows_writes_nothing(conn):
    data = plan.extract({})
    n = plan.record(conn, data, now_iso=_iso(BASE))
    assert n == 0
    total = conn.execute("SELECT COUNT(*) c FROM plan_snapshots").fetchone()["c"]
    assert total == 0


# --- 3. dash plan_payload -----------------------------------------------------


def test_plan_payload_empty_db_is_unavailable(conn):
    from ccmetrics.dash import server as dash_server

    body = dash_server.plan_payload(conn)
    assert body == {
        "available": False,
        "windows": [],
        "setup_cmd": "ccmetrics statusline --setup",
    }


def test_plan_payload_fresh_snapshot_available_with_windows(conn):
    from ccmetrics.dash import server as dash_server

    data = _windows_payload()
    now = _dt.datetime.now(_dt.timezone.utc)
    plan.record(conn, data, now_iso=_iso(now))

    body = dash_server.plan_payload(conn)
    assert body["available"] is True
    assert body["stale_after_hours"] == plan.STALE_HOURS
    names = {w["window"] for w in body["windows"]}
    assert names == {"five_hour", "seven_day"}
    for w in body["windows"]:
        assert w["stale"] is False
        assert w["used_pct"] is not None


def test_plan_payload_stale_snapshot_labelled_not_hidden(conn):
    """Read the actual contract: constants.PLAN['stale_hours'] docs say the
    dashboard keeps showing an old reading but labels its age -- it is not
    dropped from availability the way the statusline hides it."""
    from ccmetrics.dash import server as dash_server

    data = _windows_payload()
    now = _dt.datetime.now(_dt.timezone.utc)
    old_ts = _iso(now - _dt.timedelta(hours=plan.STALE_HOURS + 1))
    plan.record(conn, data, now_iso=old_ts)

    body = dash_server.plan_payload(conn)
    assert body["available"] is True  # still shown, just marked
    assert body["windows"], "expected the stale snapshot to still be returned"
    for w in body["windows"]:
        assert w["stale"] is True


# --- 4. report.plan_line ------------------------------------------------------


def _snapshot_windows(now: _dt.datetime, age_hours: float = 0.0):
    ts = _iso(now - _dt.timedelta(hours=age_hours))
    return {
        "five_hour": {"used_pct": 31.0, "resets_at": _iso(now + _dt.timedelta(hours=2)), "ts": ts},
        "seven_day": {"used_pct": 62.0, "resets_at": _iso(now + _dt.timedelta(days=3)), "ts": ts},
    }


def test_plan_line_fresh_contains_wk_and_5h_percentages():
    now = _dt.datetime.now(_dt.timezone.utc)
    line = report.plan_line(_snapshot_windows(now))
    assert line is not None
    assert "5h 31%" in line
    assert "wk 62%" in line


def test_plan_line_stale_is_hidden():
    now = _dt.datetime.now(_dt.timezone.utc)
    windows = _snapshot_windows(now, age_hours=plan.STALE_HOURS + 1)
    assert report.plan_line(windows) is None


def test_plan_line_no_windows_is_none():
    assert report.plan_line(None) is None
    assert report.plan_line({}) is None


# --- 5. v3 -> v4 migration: plan_snapshots table ------------------------------


def test_v3_to_v4_migration_creates_plan_snapshots_no_data_loss(cc_env):
    # Build a real store (creates the full current schema, plan_snapshots
    # included), seed unrelated data, then drop plan_snapshots to simulate a
    # store built before phase 5 ever existed. Reconnecting must recreate the
    # table without disturbing anything else.
    c = store.connect(cc_env["db_path"])
    store.upsert_daily(
        c,
        {
            "project": "-Users-old-proj",
            "date": "2026-07-01",
            "floor_usd": 1.23,
            "cw5m": 100,
            "cw1h": 0,
            "cread": 50,
            "out_bytes": 10,
            "turns": 5,
        },
    )
    c.commit()
    c.close()

    raw = sqlite3.connect(str(cc_env["db_path"]))
    raw.execute("DROP TABLE plan_snapshots")
    raw.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    raw.commit()
    raw.close()

    c2 = store.connect(cc_env["db_path"])
    try:
        tables = {r["name"] for r in c2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "plan_snapshots" in tables

        row = c2.execute(
            "SELECT turns, cread FROM daily WHERE project=? AND date=?",
            ("-Users-old-proj", "2026-07-01"),
        ).fetchone()
        assert row is not None
        assert row["turns"] == 5
        assert row["cread"] == 50

        assert store.get_meta(c2, "schema_version") == str(store.SCHEMA_VERSION)

        # the recreated table is fully usable
        data = _windows_payload()
        n = plan.record(c2, data, now_iso=_iso(BASE))
        assert n == 2
    finally:
        c2.close()
