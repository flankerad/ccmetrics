"""Optional plan-limit feed (`ccmetrics statusline`): extract whitelist +
sanitization, record throttle, dash /api/plan payload + staleness contract,
report PLAN line, and the v3->v4 plan_snapshots migration."""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

import json

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


# --- 6. plan trend: replayed measurements, never invented --------------------


def _insert_snapshot(conn, ts: str, window_key: str, used_pct):
    conn.execute(
        "INSERT INTO plan_snapshots (ts, window_key, used_pct, resets_at, session_id) "
        "VALUES (?, ?, ?, NULL, NULL)",
        (ts, window_key, used_pct),
    )


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


def test_passthrough_prints_the_wrapped_commands_output_not_our_own_line():
    assert plan.run("{}", passthrough="printf theirs") == "theirs"


def test_passthrough_is_handed_the_same_stdin_and_the_snapshot_is_still_stored(conn):
    reset = BASE + _dt.timedelta(hours=2)
    payload = json.dumps({
        "session_id": "sess-chain",
        "rate_limits": {"five_hour": {"used_percentage": 31, "resets_at": _epoch(reset)}},
    })

    out = plan.run(payload, passthrough="cat")

    assert json.loads(out) == json.loads(payload)  # the child saw the payload
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
    assert plan.run("{}", passthrough=cmd) == "ccmetrics"


def test_passthrough_slow_command_times_out_and_falls_back(monkeypatch):
    monkeypatch.setattr(plan, "PASSTHROUGH_TIMEOUT_S", 0.2)
    assert plan.run("{}", passthrough="sleep 5") == "ccmetrics"


def test_cli_statusline_with_a_broken_passthrough_prints_a_line_and_exits_0(
    cc_env, monkeypatch, capsys
):
    import io
    import sys

    from ccmetrics.cli import main

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert main(["statusline", "--passthrough", "ccmetrics-no-such-command-xyz"]) == 0
    assert capsys.readouterr().out.strip() == "ccmetrics"


def test_setup_text_shows_both_the_plain_and_the_chained_fragment():
    text = plan.setup_text()
    assert chr(34) + "command" + chr(34) + ": " + chr(34) + "ccmetrics statusline" + chr(34) in text
    assert "--passthrough" in text
    assert "Claude Code runs exactly one command" in text
