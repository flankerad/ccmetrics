"""Optional plan-limit feed (`ccmetrics statusline`): extract whitelist +
sanitization, record throttle, dash /api/plan payload + staleness contract,
report PLAN line, and the v3->v4 plan_snapshots migration."""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

import json

from ccmetrics import plan, report, store


def _plain(line: str) -> str:
    """The status line without its colour escapes."""
    import re as _re

    return _re.sub(r"\033\[[0-9;]*m", "", line)

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


# --- 6. plan trend: replayed measurements, never invented --------------------


def _insert_snapshot(conn, ts: str, window_key: str, used_pct, resets_at=None):
    conn.execute(
        "INSERT INTO plan_snapshots (ts, window_key, used_pct, resets_at, session_id) "
        "VALUES (?, ?, ?, ?, NULL)",
        (ts, window_key, used_pct, resets_at),
    )


# --- latest_plan_windows: a reset window never outranks the current one (D58) -
#
# `now_iso` is passed explicitly in every test below (BASE is 2026-07-31, long
# before the real wall clock the store defaults to) so "expired" is judged
# against the fabricated scenario's own clock, not whatever day the test
# happens to run on.

def test_latest_plan_windows_expired_but_newer_ts_loses_to_the_fresh_window(conn):
    """The exact bug: one session keeps rewriting a pre-reset snapshot every
    ~20s (newest `ts`, but its own window already reset); another session
    reports the true post-reset reading with an older `ts`. The fresh
    window must win regardless of which `ts` is newer."""
    now = BASE
    fresh_reset = BASE + _dt.timedelta(days=7)  # still ahead of `now` -- live
    stale_reset = BASE - _dt.timedelta(hours=4)  # already behind `now` -- expired

    _insert_snapshot(conn, _iso(BASE), "seven_day", 1.0, resets_at=_iso(fresh_reset))
    _insert_snapshot(
        conn, _iso(BASE + _dt.timedelta(minutes=1)), "seven_day", 96.0,
        resets_at=_iso(stale_reset),
    )
    conn.commit()

    latest = store.latest_plan_windows(conn, now_iso=_iso(now))
    assert latest["seven_day"]["used_pct"] == pytest.approx(1.0)
    assert latest["seven_day"]["resets_at"] == _iso(fresh_reset)


def test_latest_plan_windows_all_null_resets_at_still_picks_max_ts(conn):
    """No `resets_at` at all on either row (plan.extract kept them anyway) --
    neither is ever "expired", so it falls back to plain max-`ts`."""
    _insert_snapshot(conn, _iso(BASE), "five_hour", 10.0)
    _insert_snapshot(conn, _iso(BASE + _dt.timedelta(minutes=1)), "five_hour", 25.0)
    conn.commit()

    latest = store.latest_plan_windows(conn, now_iso=_iso(BASE))
    assert latest["five_hour"]["used_pct"] == pytest.approx(25.0)


def test_latest_plan_windows_fresh_null_resets_at_beats_a_stale_dated_row(conn):
    """GAP 1: sorting NULL `resets_at` last unconditionally was itself a bug --
    a stale-but-dated row must not bury a fresher NULL one. Session A writes
    the true current reading but its resets_at epoch didn't parse; session B
    holds an older, already-expired dated reading. A wins on `ts` alone,
    same tier as any other live row."""
    now = BASE
    stale_reset = BASE - _dt.timedelta(hours=6)  # already behind `now` -- expired

    _insert_snapshot(  # session B: stale, dated, older ts
        conn, _iso(BASE - _dt.timedelta(hours=6)), "seven_day", 12.0,
        resets_at=_iso(stale_reset),
    )
    _insert_snapshot(  # session A: fresh, no resets_at, newer ts
        conn, _iso(BASE), "seven_day", 90.0, resets_at=None,
    )
    conn.commit()

    latest = store.latest_plan_windows(conn, now_iso=_iso(now))
    assert latest["seven_day"]["used_pct"] == pytest.approx(90.0)
    assert latest["seven_day"]["resets_at"] is None


def test_latest_plan_windows_returns_a_lone_expired_reading(conn):
    """The store still reports an expired reading when nothing fresher has
    arrived yet -- withholding it as 'unknown' is windows._headroom's H4
    call to make, not the store's."""
    now = BASE
    expired = BASE - _dt.timedelta(hours=4)
    _insert_snapshot(conn, _iso(BASE), "seven_day", 96.0, resets_at=_iso(expired))
    conn.commit()

    latest = store.latest_plan_windows(conn, now_iso=_iso(now))
    assert latest["seven_day"]["used_pct"] == pytest.approx(96.0)
    assert latest["seven_day"]["resets_at"] == _iso(expired)


def test_plan_payload_empty_store_has_no_trend_key_change(conn):
    """USER story 3: without snapshots the payload is exactly the old empty
    shape -- the page's hint contract, asserted whole."""
    from ccmetrics.dash import server as dash_server

    body = dash_server.plan_payload(conn)
    assert body == {
        "available": False,
        "windows": [],
        "setup_cmd": "ccmetrics statusline --setup",
    }


def test_plan_trend_replays_stored_points_in_order(conn):
    """USER story 3: the trend is the stored readings, in ts order, values
    untouched -- no rounding, no interpolation, no extra windows."""
    from ccmetrics.dash import server as dash_server

    now = _dt.datetime.now(_dt.timezone.utc)
    for hours_ago, pct in ((3, 10.0), (2, 12.5), (1, 20.0)):
        _insert_snapshot(conn, _iso(now - _dt.timedelta(hours=hours_ago)), "seven_day", pct)
    _insert_snapshot(conn, _iso(now - _dt.timedelta(hours=1)), "five_hour", 55.0)
    conn.commit()

    body = dash_server.plan_payload(conn)
    assert body["available"] is True
    trend = body["trend"]
    assert set(trend) == {"seven_day", "five_hour"}
    assert [p["used_pct"] for p in trend["seven_day"]] == [10.0, 12.5, 20.0]
    assert [p["ts"] for p in trend["seven_day"]] == sorted(
        p["ts"] for p in trend["seven_day"]
    )
    assert trend["five_hour"] == [
        {"ts": _iso(now - _dt.timedelta(hours=1)), "used_pct": 55.0}
    ]


def test_plan_trend_caps_at_newest_100_per_window(conn):
    now = _dt.datetime.now(_dt.timezone.utc)
    for i in range(105):
        _insert_snapshot(
            conn, _iso(now - _dt.timedelta(minutes=105 - i)), "seven_day", float(i)
        )
    conn.commit()

    trend = store.plan_window_trend(conn)
    pts = trend["seven_day"]
    assert len(pts) == 100
    # the oldest 5 readings (pct 0..4) fell off; the newest survive in order
    assert pts[0]["used_pct"] == 5.0
    assert pts[-1]["used_pct"] == 104.0


def test_plan_trend_null_reading_stays_null(conn):
    """A reading Claude Code sent without a percentage is absent data -- the
    API must hand it over as None, never coerce it to 0."""
    now = _dt.datetime.now(_dt.timezone.utc)
    _insert_snapshot(conn, _iso(now - _dt.timedelta(hours=2)), "seven_day", None)
    _insert_snapshot(conn, _iso(now - _dt.timedelta(hours=1)), "seven_day", 30.0)
    conn.commit()

    trend = store.plan_window_trend(conn)
    assert [p["used_pct"] for p in trend["seven_day"]] == [None, 30.0]


# --- read_usage_cache: the /usage cache (~/.claude.json) --------------------

def _write_cache(path, utilization=None, oauth=None, fetched_ms=1785477153001):
    body = {}
    cu = {"fetchedAtMs": fetched_ms, "utilization": utilization if utilization is not None else {}}
    body["cachedUsageUtilization"] = cu
    if oauth is not None:
        body["oauthAccount"] = oauth
    path.write_text(json.dumps(body))

def test_read_usage_cache_limits_session_weekly_all_and_scoped_fable(tmp_path):
    p = tmp_path / "claude.json"
    _write_cache(p, utilization={
        "limits": [
            {"kind": "session", "group": "session", "percent": 18, "severity": "normal",
             "resets_at": "2026-07-31T15:00:00.868761+00:00", "scope": None, "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 85, "severity": "normal",
             "resets_at": "2026-08-03T12:00:00.868783+00:00", "scope": None, "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 94, "severity": "normal",
             "resets_at": "2026-08-03T12:00:00.868783+00:00", "is_active": True,
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
    })

    out = plan.read_usage_cache(p)
    assert out is not None
    expected_fetched = _dt.datetime.fromtimestamp(
        1785477153001 / 1000.0, _dt.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert out["fetched_at"] == expected_fetched

    windows = out["windows"]
    assert list(windows) == ["session", "weekly_all", "weekly_scoped_fable"]
    assert windows["session"]["label"] == "Current session"
    assert windows["session"]["used_pct"] == pytest.approx(18.0)
    assert windows["weekly_all"]["label"] == "This week · all models"
    assert windows["weekly_all"]["used_pct"] == pytest.approx(85.0)
    assert windows["weekly_scoped_fable"]["label"] == "This week · Fable"
    assert windows["weekly_scoped_fable"]["model"] == "Fable"
    assert windows["weekly_scoped_fable"]["used_pct"] == pytest.approx(94.0)

def test_read_usage_cache_unknown_kind_kept_under_raw_label():
    windows = plan._extract_bars_list([
        {"kind": "seven_day_opus", "percent": 40, "resets_at": None, "is_active": True},
    ])
    assert set(windows) == {"seven_day_opus"}
    assert windows["seven_day_opus"]["label"] == "seven day opus"
    assert windows["seven_day_opus"]["used_pct"] == pytest.approx(40.0)

def test_read_usage_cache_bars_skip_missing_or_nonnumeric_percent():
    windows = plan._extract_bars_list([
        {"kind": "session", "resets_at": None, "is_active": True},  # no percent at all
        {"kind": "weekly_all", "percent": "n/a", "resets_at": None, "is_active": True},
        {"kind": "weekly_all", "percent": 50, "resets_at": None, "is_active": True},
    ])
    assert set(windows) == {"weekly_all"}
    assert windows["weekly_all"]["used_pct"] == pytest.approx(50.0)

def test_read_usage_cache_missing_bars_falls_back_to_named_keys_nulls_ignored(tmp_path):
    p = tmp_path / "claude.json"
    _write_cache(p, utilization={
        "five_hour": {"utilization": 7, "resets_at": "2026-07-31T10:40:00.868761+00:00"},
        "seven_day": None,
        "seven_day_opus": None,
    })
    out = plan.read_usage_cache(p)
    assert out is not None
    assert set(out["windows"]) == {"five_hour"}
    assert out["windows"]["five_hour"]["used_pct"] == pytest.approx(7.0)
    assert out["windows"]["five_hour"]["label"] == "last 5 hours"

def test_read_usage_cache_junk_paths_return_none_never_raise(tmp_path):
    missing = tmp_path / "nope.json"
    assert plan.read_usage_cache(missing) is None

    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert plan.read_usage_cache(empty) is None

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json")
    assert plan.read_usage_cache(malformed) is None

    no_block = tmp_path / "noblock.json"
    no_block.write_text(json.dumps({"oauthAccount": {}}))
    assert plan.read_usage_cache(no_block) is None

def test_plan_tier_known_key_maps_and_unknown_returns_none(tmp_path):
    known = tmp_path / "known.json"
    known.write_text(json.dumps({
        "oauthAccount": {"organizationRateLimitTier": "default_claude_max_5x",
                          "organizationType": "claude_max"},
    }))
    assert plan.plan_tier(known) == {
        "key": "default_claude_max_5x", "label": "Max (5×)", "type": "claude_max",
    }

    unknown = tmp_path / "unknown.json"
    unknown.write_text(json.dumps({"oauthAccount": {"organizationRateLimitTier": "mystery_tier"}}))
    assert plan.plan_tier(unknown) is None

    absent = tmp_path / "absent.json"
    assert plan.plan_tier(absent) is None

def test_window_labels_has_no_per_model_entry():
    assert "seven_day_opus" not in plan.WINDOW_LABELS
    assert all("opus" not in k and "sonnet" not in k and "haiku" not in k
               for k in plan.WINDOW_LABELS)

def test_read_usage_cache_bars_accepted_as_secondary_name(tmp_path):
    """PLAN-dash-v2 1.1 correction: the real file keys this list `limits`;
    `bars` is a secondary name kept only for future-proofing, so it must not
    rot -- a payload carrying only `bars` still parses."""
    p = tmp_path / "claude.json"
    _write_cache(p, utilization={
        "bars": [
            {"kind": "session", "percent": 12, "resets_at": None, "is_active": True},
        ],
    })
    out = plan.read_usage_cache(p)
    assert out is not None
    assert set(out["windows"]) == {"session"}
    assert out["windows"]["session"]["used_pct"] == pytest.approx(12.0)

def test_read_usage_cache_limits_preferred_over_bars_when_both_present(tmp_path):
    p = tmp_path / "claude.json"
    _write_cache(p, utilization={
        "limits": [{"kind": "session", "percent": 40, "resets_at": None, "is_active": True}],
        "bars": [{"kind": "session", "percent": 99, "resets_at": None, "is_active": True}],
    })
    out = plan.read_usage_cache(p)
    assert out["windows"]["session"]["used_pct"] == pytest.approx(40.0)


# --- record_usage_cache: snapshot on a new fetch time ------------------------

def test_record_usage_cache_writes_rows_and_skips_unchanged(conn):
    p = plan.usage_cache_path()
    _write_cache(p, utilization={
        "limits": [{"kind": "session", "percent": 20, "resets_at": None, "is_active": True}],
    }, fetched_ms=1785477153001)

    n = plan.record_usage_cache(conn)
    assert n == 1
    latest = store.latest_plan_windows(conn)
    assert latest["session"]["used_pct"] == pytest.approx(20.0)

    # unchanged cache, same fetchedAtMs -- writes zero rows
    n2 = plan.record_usage_cache(conn)
    assert n2 == 0

def test_record_usage_cache_newer_fetch_writes_a_new_row(conn):
    p = plan.usage_cache_path()
    _write_cache(p, utilization={
        "limits": [{"kind": "session", "percent": 20, "resets_at": None, "is_active": True}],
    }, fetched_ms=1785477153001)
    plan.record_usage_cache(conn)

    _write_cache(p, utilization={
        "limits": [{"kind": "session", "percent": 33, "resets_at": None, "is_active": True}],
    }, fetched_ms=1785477153001 + 60000)
    n = plan.record_usage_cache(conn)
    assert n == 1
    latest = store.latest_plan_windows(conn)
    assert latest["session"]["used_pct"] == pytest.approx(33.0)


# --- usage_credits: utilization.extra_usage ----------------------------------

def test_usage_credits_reads_extra_usage(tmp_path):
    p = tmp_path / "claude.json"
    _write_cache(p, utilization={
        "extra_usage": {
            "is_enabled": False, "monthly_limit": 500, "used_credits": 0,
            "utilization": 0, "currency": "USD", "decimal_places": 2,
            "disabled_reason": "org_level_disabled_until",
        },
    })
    out = plan.usage_credits(p)
    assert out == {
        "is_enabled": False, "monthly_limit": 500, "used_credits": 0,
        "utilization": 0, "currency": "USD", "decimal_places": 2,
        "disabled_reason": "org_level_disabled_until",
    }

def test_usage_credits_none_when_extra_usage_absent(tmp_path):
    p = tmp_path / "claude.json"
    _write_cache(p, utilization={"spend": {"used": {}, "limit": {}}})
    assert plan.usage_credits(p) is None

def test_usage_credits_none_on_missing_or_malformed_file(tmp_path):
    assert plan.usage_credits(tmp_path / "nope.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert plan.usage_credits(bad) is None


# --- statusline chaining: `ccmetrics statusline --passthrough "..."` ---------
# Claude Code allows exactly one status line command, and the payload it hands
# that command is the only continuously-fresh source of plan percentages. So
# ccmetrics has to be able to own the slot without taking it away from whatever
# the user already had there.


def test_passthrough_prints_the_wrapped_commands_output_then_our_own_line():
    """Both lines are shown: theirs first, ours after the separator."""
    import os

    out = _plain(plan.run("{}", passthrough="printf theirs"))
    assert out == "theirs | " + os.path.basename(os.getcwd())


def test_several_passthroughs_all_appear_in_order():
    out = _plain(plan.run("{}", passthrough=["printf one", "printf two"]))
    assert out.startswith("one | two | ")


def test_passthrough_is_handed_the_same_stdin_and_the_snapshot_is_still_stored(conn):
    reset = BASE + _dt.timedelta(hours=2)
    payload = json.dumps({
        "session_id": "sess-chain",
        "rate_limits": {"five_hour": {"used_percentage": 31, "resets_at": _epoch(reset)}},
    })

    out = plan.run(payload, passthrough="cat")

    seen = _plain(out).split(" | ")[0]
    assert json.loads(seen) == json.loads(payload)  # the child saw the payload
    latest = store.latest_plan_windows(conn)       # and ccmetrics still recorded it
    assert latest["five_hour"]["used_pct"] == pytest.approx(31.0)


@pytest.mark.parametrize("cmd", [
    "ccmetrics-no-such-command-xyz",  # missing
    "exit 3",                         # non-zero exit
    "true",                           # exits 0, prints nothing
    "   ",                            # empty command string
])
def test_passthrough_failure_modes_fall_back_to_our_own_line(cmd):
    """A status line is the user's editor chrome: it may never go blank or
    loud, so every way the wrapped command can let us down ends in ccmetrics'
    own line instead."""
    import os

    assert _plain(plan.run("{}", passthrough=cmd)) == os.path.basename(os.getcwd())


def test_passthrough_slow_command_times_out_and_falls_back(monkeypatch):
    monkeypatch.setattr(plan, "PASSTHROUGH_TIMEOUT_S", 0.2)
    import os

    assert _plain(plan.run("{}", passthrough="sleep 5")) == os.path.basename(os.getcwd())


def test_cli_statusline_with_a_broken_passthrough_prints_a_line_and_exits_0(
    cc_env, monkeypatch, capsys
):
    import io
    import sys

    from ccmetrics.cli import main

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert main(["statusline", "--passthrough", "ccmetrics-no-such-command-xyz"]) == 0
    import os

    assert _plain(capsys.readouterr().out.strip()) == os.path.basename(os.getcwd())


def test_setup_text_shows_both_the_plain_and_the_chained_fragment():
    text = plan.setup_text()
    assert chr(34) + "command" + chr(34) + ": " + chr(34) + "ccmetrics statusline" + chr(34) in text
    assert "--passthrough" in text
    assert "Claude Code runs exactly one command" in text


# --- _ctx_bar: sub-cell resolution so the bar actually moves -----------------


def test_ctx_bar_zero_percent_is_fully_dim_no_sliver():
    assert _plain(plan._ctx_bar(0)) == "░░░░░░░░"


def test_ctx_bar_one_percent_shows_a_visible_sliver():
    bar = _plain(plan._ctx_bar(1))
    assert bar != "░░░░░░░░"
    assert bar[0] == "▏"
    assert bar[1:] == "░░░░░░░"


def test_ctx_bar_partial_cell_is_coloured_by_its_own_position_not_overall_pct(monkeypatch):
    # 1% only fills cell 0 (its own heat stop is 12.5%), not _heat(1.0).
    monkeypatch.delenv("NO_COLOR", raising=False)
    raw = plan._ctx_bar(1)
    own_position_colour = plan._heat(1 / 8 * 100.0)
    overall_pct_colour = plan._heat(1.0)
    assert own_position_colour != overall_pct_colour
    assert raw.startswith(own_position_colour)


def test_ctx_bar_nan_is_treated_as_zero_not_a_crash():
    assert _plain(plan._ctx_bar(float("nan"))) == "░░░░░░░░"


def test_ctx_bar_boundary_just_below_one_full_cell_is_a_partial_glyph():
    bar = _plain(plan._ctx_bar(11))
    assert bar[0] == "▉"
    assert bar[1:] == "░░░░░░░"


def test_ctx_bar_boundary_at_or_above_one_full_cell_is_a_full_block():
    bar = _plain(plan._ctx_bar(13))
    assert bar[0] == "█"
    assert bar[1:] == "░░░░░░░"


def test_ctx_bar_fifty_percent_is_four_full_cells_no_partial():
    assert _plain(plan._ctx_bar(50)) == "████░░░░"


def test_ctx_bar_ninety_nine_percent_is_seven_full_and_a_near_full_sliver():
    assert _plain(plan._ctx_bar(99)) == "███████▉"


def test_ctx_bar_hundred_percent_is_eight_full_blocks_no_ninth_cell():
    bar = _plain(plan._ctx_bar(100))
    assert bar == "████████"
    assert len(bar) == 8


def test_ctx_bar_width_is_constant_across_the_full_range():
    for tenth in range(0, 1001):
        pct = tenth / 10.0
        bar = _plain(plan._ctx_bar(pct))
        assert len(bar) == 8, f"pct={pct} bar={bar!r}"


def test_ctx_bar_fill_weight_never_decreases_as_pct_rises():
    weight = {c: i for i, c in enumerate("░" + plan._EIGHTHS + "█")}

    def total(pct):
        return sum(weight[c] for c in _plain(plan._ctx_bar(pct)))

    prev = -1
    for tenth in range(0, 1001):
        pct = tenth / 10.0
        cur = total(pct)
        assert cur >= prev, f"pct={pct} weight dropped from {prev} to {cur}"
        prev = cur


# --- render_line: window segments show what's LEFT, not what's used ----------


def test_render_line_window_segments_show_percent_left_not_used():
    line = _plain(plan.render_line({"windows": {"five_hour": {"used_pct": 70}, "seven_day": {"used_pct": 81}}}))
    assert "5h 30% left" in line
    assert "week 19% left" in line


def test_render_line_zero_used_is_hundred_percent_left():
    line = _plain(plan.render_line({"windows": {"five_hour": {"used_pct": 0}}}))
    assert "5h 100% left" in line


def test_render_line_hundred_used_is_zero_percent_left():
    line = _plain(plan.render_line({"windows": {"five_hour": {"used_pct": 100}}}))
    assert "5h 0% left" in line


def test_render_line_seven_day_label_reads_week_not_wk():
    line = _plain(plan.render_line({"windows": {"seven_day": {"used_pct": 50}}}))
    assert "week 50% left" in line
    assert "wk " not in line
    # WINDOW_LABELS itself must stay untouched -- console/dash still say "wk".
    assert plan.WINDOW_LABELS["seven_day"][0] == "wk"


def test_render_line_missing_used_pct_skips_the_segment():
    line = _plain(plan.render_line({"windows": {"five_hour": {}}}))
    # No segment emitted -> parts stays empty -> same fallback as no data at all.
    assert line == _plain(plan.render_line({}))


def test_render_line_heat_stays_keyed_to_used_pct_not_left_pct():
    """A window at 95% used has only 5% left to print, but the colour must
    still be graded as nearly-exhausted (i.e. the same red _heat(95) would
    give directly) -- not the near-empty green that _heat(5) would give."""
    line = plan.render_line({"windows": {"five_hour": {"used_pct": 95}}})
    assert plan._heat(95) in line
    assert plan._heat(5) not in line


# --- render_line: the bar tracks the weekly window, not context --------------

def test_render_line_bar_sits_beside_the_week_segment_not_context():
    """Item 1 follow-up, session 2026-08-09: the bar tracks the weekly plan
    window's used_pct, and now sits where that number actually prints --
    immediately before 'week NN% left', at the end of the line -- rather
    than in the model/context area it never described. Context text stays
    plain, no bar attached, no matter how low or high context itself is."""
    line = _plain(plan.render_line({
        "ctx_used": 60_000, "ctx_max": 1_000_000, "context_pct": 6,
        "windows": {"seven_day": {"used_pct": 82}},
    }))
    assert f"{_plain(plan._ctx_bar(82))} week 18% left" in line
    # No bar glyph anywhere near the context segment -- it moved, not copied.
    ctx_segment = next(seg for seg in line.split("|") if "60k/1M" in seg)
    assert ctx_segment.strip() == "60k/1M (6%)"


def _no_bar_glyphs(line: str) -> bool:
    return not any(g in line for g in plan._EIGHTHS + "█░")

def test_render_line_no_bar_at_all_with_no_weekly_reading():
    """A non-subscriber (or any payload with no seven_day window at all) has
    no weekly number to feed the bar. It prints no bar anywhere on the line
    -- it does NOT fall back to painting the context reading instead."""
    line = _plain(plan.render_line({"ctx_used": 60_000, "ctx_max": 1_000_000, "context_pct": 6}))
    assert _no_bar_glyphs(line)
    ctx_segment = next(seg for seg in line.split("|") if "60k/1M" in seg)
    assert ctx_segment.strip() == "60k/1M (6%)"


def test_render_line_no_bar_when_week_present_but_five_hour_only_reading():
    """The bar is keyed to the "week" LABEL, not the raw "seven_day" key --
    but a five_hour-only reading has no window that prints as "week" at all,
    so it still gets no bar, even though SOME window segment is present."""
    line = _plain(plan.render_line({
        "ctx_used": 60_000, "ctx_max": 1_000_000, "context_pct": 6,
        "windows": {"five_hour": {"used_pct": 8}},
    }))
    assert _no_bar_glyphs(line)
    assert "5h 92% left" in line


def test_render_line_two_weekly_windows_each_get_their_own_bar():
    """Scoping decision, session 2026-08-09: the label gate (short == "week")
    lives inside the per-window loop with no once-only guard, so a payload
    carrying more than one window that prints as "week" (seven_day AND
    weekly_all both map to "wk"/"week" in WINDOW_LABELS) gets a bar on EACH
    of those segments, not just the first. Each bar still describes only
    its own row's used_pct -- deliberate, not a regression of the old
    seven_day-only rule, which could only ever have had one weekly row to
    begin with since the statusline hook feed never sends both at once."""
    line = _plain(plan.render_line({"windows": {
        "seven_day": {"used_pct": 82},
        "weekly_all": {"used_pct": 59},
    }}))
    assert f"{_plain(plan._ctx_bar(82))} week 18% left" in line
    assert f"{_plain(plan._ctx_bar(59))} week 41% left" in line


def test_render_line_bar_attaches_to_any_window_that_prints_as_week():
    """GAP 2 fix, session 2026-08-09: the bar is gated on the printed label
    ("week"), not the literal "seven_day" key -- weekly_all (a name the
    /usage-cache format uses, distinct from the statusline hook's
    "seven_day") still gets the bar since it prints the same "week" label."""
    line = _plain(plan.render_line({"windows": {"weekly_all": {"used_pct": 82}}}))
    assert f"{_plain(plan._ctx_bar(82))} week 18% left" in line
