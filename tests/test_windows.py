"""cap_estimates delta/absolute estimator + snapshot recording (PLAN-dash-v2 4).

Fixtures only, env-overridden paths (conftest.cc_env) -- never the real
~/.claude.json or DB.
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest
from unittest import mock

from ccmetrics import plan, store, windows

BASE = _dt.datetime(2026, 8, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)


def _turn(conn, ts, cread, model="claude-sonnet-5"):
    conn.execute(
        "INSERT INTO turns(session_id, project, ts, model, cw5m, cw1h, cread, "
        "raw_in, raw_out, out_bytes) VALUES('s','p',?,?,0,0,?,0,0,0)",
        (ts, model, cread),
    )


def _snap(conn, ts, window_key, used_pct, resets_at=None):
    conn.execute(
        "INSERT INTO plan_snapshots(ts, window_key, used_pct, resets_at, session_id) "
        "VALUES(?,?,?,?,NULL)",
        (ts, window_key, used_pct, resets_at),
    )


def _set_last_ingest(conn, dt):
    store.set_meta(conn, "last_ingest", windows.iso(dt))
    conn.commit()


# --- snapshot recording (via plan.record_usage_cache, on the same table
# cap_estimates reads) ---------------------------------------------------

def _write_cache(path, fetched_ms, pct):
    body = {
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched_ms,
            "utilization": {
                "limits": [
                    {"kind": "session", "percent": pct, "resets_at": None, "is_active": True},
                ],
            },
        }
    }
    path.write_text(json.dumps(body))


def test_source_kind_hook_or_none_never_cache_or_both(conn):
    """Session 2026-08-09, item 4: the /usage cache was dropped as a source
    entirely -- `_source_kind` no longer distinguishes cache/hook/both, just
    whether the status-line feed (`plan_snapshots`) has written anything."""
    assert windows._source_kind(conn) is None
    _snap(conn, windows.iso(BASE), "five_hour", 10.0, None)
    conn.commit()
    assert windows._source_kind(conn) == "hook"


def test_snapshot_written_once_unchanged_zero_newer_writes_new_row(conn):
    p = plan.usage_cache_path()
    _write_cache(p, 1785477153001, 20)
    assert plan.record_usage_cache(conn) == 1
    assert plan.record_usage_cache(conn) == 0  # unchanged cache -- no new row

    _write_cache(p, 1785477153001 + 60000, 33)
    assert plan.record_usage_cache(conn) == 1  # newer fetchedAtMs -- new row
    latest = store.latest_plan_windows(conn)
    assert latest["session"]["used_pct"] == pytest.approx(33.0)


# --- cap_estimates: delta form -------------------------------------------

def test_cap_estimates_delta_maths_on_a_clean_pair(conn):
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    reset = BASE + _dt.timedelta(hours=5)

    _turn(conn, windows.iso(t0 - _dt.timedelta(hours=1)), 500)  # before the pair: irrelevant
    _turn(conn, windows.iso(t0 + _dt.timedelta(minutes=30)), 2000)  # inside [t0, t1]: 200 equiv

    _snap(conn, windows.iso(t0), "session", 10.0, windows.iso(reset))
    _snap(conn, windows.iso(t1), "session", 20.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    assert caps["session"]["cap_equiv"] == pytest.approx(2000.0)
    assert caps["session"]["samples"] == 1
    assert caps["session"]["confidence"] == "single"


def test_cap_estimates_reset_straddling_pair_rejected(conn):
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    _snap(conn, windows.iso(t0), "session", 1.0, windows.iso(BASE + _dt.timedelta(hours=5)))
    _snap(conn, windows.iso(t1), "session", 3.0, windows.iso(BASE + _dt.timedelta(hours=6)))
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    assert "session" not in caps  # both below the abs floor too, so no fallback either


def test_cap_estimates_small_delta_pct_rejected(conn):
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    reset = BASE + _dt.timedelta(hours=5)
    _snap(conn, windows.iso(t0), "session", 1.0, windows.iso(reset))
    _snap(conn, windows.iso(t1), "session", 1.5, windows.iso(reset))  # delta 0.5, below 2
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    assert "session" not in caps  # both below the abs floor too, so no fallback either


def test_cap_estimates_percent_decrease_pair_rejected(conn):
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    reset = BASE + _dt.timedelta(hours=5)
    _turn(conn, windows.iso(t0 - _dt.timedelta(minutes=30)), 5000)
    _turn(conn, windows.iso(t1 - _dt.timedelta(minutes=30)), 5000)
    _snap(conn, windows.iso(t0), "session", 20.0, windows.iso(reset))
    _snap(conn, windows.iso(t1), "session", 15.0, windows.iso(reset))  # went down
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    # the pair is not usable, so both readings fall back to the absolute form
    # individually instead of the (rejected) delta of the pair
    assert caps["session"]["samples"] == 2


def test_cap_estimates_snapshot_newer_than_last_ingest_rejected(conn):
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    reset = BASE + _dt.timedelta(hours=5)
    _turn(conn, windows.iso(t0 - _dt.timedelta(minutes=30)), 1000)
    _snap(conn, windows.iso(t0), "session", 10.0, windows.iso(reset))
    _snap(conn, windows.iso(t1), "session", 20.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, t0)  # t1 is newer than last_ingest -- dropped

    caps = windows.cap_estimates(conn)
    assert caps["session"]["samples"] == 1
    assert caps["session"]["confidence"] == "single"


def test_cap_estimates_median_not_mean_with_one_outlier(conn):
    ends = [BASE, BASE + _dt.timedelta(days=30), BASE + _dt.timedelta(days=60)]
    creads = [1000, 1000, 100000]
    for end, cread in zip(ends, creads):
        _turn(conn, windows.iso(end - _dt.timedelta(days=1)), cread)
        _snap(conn, windows.iso(end), "weekly_all", 10.0, None)  # equal pct: every pair rejected
    conn.commit()
    _set_last_ingest(conn, ends[-1])

    caps = windows.cap_estimates(conn)
    assert caps["weekly_all"]["samples"] == 3
    assert caps["weekly_all"]["confidence"] == "rough"
    assert caps["weekly_all"]["cap_equiv"] == pytest.approx(1000.0)  # median, not the ~34k mean


def test_cap_estimates_good_confidence_needs_five_runs(conn):
    """Under the widest-pair estimator (PLAN-cap-and-chrome Part 1) one
    unbroken run -- however many readings arrive within it -- can only ever
    give ONE delta sample: its widest pair. "Good" confidence therefore now
    means five separate observed windows (five distinct reset instants), not
    five adjacent pairs skimmed off one ongoing window."""
    for i in range(5):
        t0 = BASE + _dt.timedelta(hours=6 * i)
        t1 = t0 + _dt.timedelta(hours=1)
        reset = t0 + _dt.timedelta(hours=5)
        _turn(conn, windows.iso(t0 + _dt.timedelta(minutes=30)), 2000)
        _snap(conn, windows.iso(t0), "session", 10.0, windows.iso(reset))
        _snap(conn, windows.iso(t1), "session", 20.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, BASE + _dt.timedelta(hours=6 * 4 + 1))

    caps = windows.cap_estimates(conn)
    assert caps["session"]["samples"] == 5
    assert caps["session"]["confidence"] == "good"


def test_cap_estimates_widest_pair_survives_a_dense_status_line_feed(conn):
    """A status-line feed fires on every redraw -- readings seconds apart,
    percentage identical between neighbours. The old adjacent-pair estimator
    starved on this (every pair below the rounding floor); the widest pair in
    the run must still recover the real cap (PLAN-cap-and-chrome Part 1)."""
    reset = BASE + _dt.timedelta(hours=5)
    readings = [(0, 10.0), (5, 10.0), (10, 10.0), (3600, 30.0), (3605, 30.0)]
    for secs, pct in readings:
        _snap(conn, windows.iso(BASE + _dt.timedelta(seconds=secs)), "session", pct, windows.iso(reset))
    _turn(conn, windows.iso(BASE + _dt.timedelta(minutes=30)), 4000)
    conn.commit()
    _set_last_ingest(conn, BASE + _dt.timedelta(seconds=3605))

    caps = windows.cap_estimates(conn)
    # widest pair: 10.0% (t=0) -> 30.0% (t=3600s), spanning the 4000-cread
    # turn -- costs.billable_input_equivalent folds cread at its own multiplier
    from ccmetrics import costs
    expected_dtok = 4000 * costs._MREAD
    assert caps["session"]["cap_equiv"] == pytest.approx(expected_dtok / 0.20)
    assert caps["session"]["samples"] == 1


def test_cap_estimates_widest_pair_refuses_to_cross_a_reset(conn):
    """Two runs, one real rollover between them -- the widest pair must never
    reach across it, even though doing so would look like the widest spread
    in the raw feed (PLAN-cap-and-chrome Part 1)."""
    reset_a = BASE + _dt.timedelta(hours=5)
    reset_b = reset_a + _dt.timedelta(hours=5)  # a whole period later -- real rollover
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)  # still run A (same reset_a)
    t2 = BASE + _dt.timedelta(hours=6)  # run B, after the rollover -- pct resets low
    _turn(conn, windows.iso(t0 + _dt.timedelta(minutes=30)), 2000)
    _snap(conn, windows.iso(t0), "session", 10.0, windows.iso(reset_a))
    _snap(conn, windows.iso(t1), "session", 20.0, windows.iso(reset_a))
    _snap(conn, windows.iso(t2), "session", 5.0, windows.iso(reset_b))  # new window, low pct
    conn.commit()
    _set_last_ingest(conn, t2)

    caps = windows.cap_estimates(conn)
    # run A's own widest pair (10% -> 20%) is used; t2 never pairs against it
    assert caps["session"]["cap_equiv"] == pytest.approx(2000.0)
    assert caps["session"]["samples"] == 1


def test_cap_estimates_widest_pair_refuses_a_downward_percentage(conn):
    """A run whose overall high point comes BEFORE its overall low point (a
    real decrease, not a mid-run dip) must be refused outright -- the widest
    values by percentage are not an honest rising pair (PLAN-cap-and-chrome
    Part 1)."""
    reset = BASE + _dt.timedelta(hours=5)
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    _turn(conn, windows.iso(t0 - _dt.timedelta(minutes=30)), 5000)
    _turn(conn, windows.iso(t1 - _dt.timedelta(minutes=30)), 5000)
    _snap(conn, windows.iso(t0), "session", 20.0, windows.iso(reset))
    _snap(conn, windows.iso(t1), "session", 15.0, windows.iso(reset))  # went down
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    # the only possible pair is refused -- falls back to the absolute form,
    # one estimate per reading, instead of a delta computed on a decrease
    assert caps["session"]["samples"] == 2


def test_cap_estimates_widest_pair_ignores_a_trailing_dip(conn):
    """The widest pair is chosen by VALUE, not by first/last position: a run
    that rises then dips before the feed stops must still use its true peak,
    not the final (lower) reading, as the high end (PLAN-cap-and-chrome
    Part 1)."""
    reset = BASE + _dt.timedelta(hours=5)
    t0, t1, t2 = BASE, BASE + _dt.timedelta(minutes=20), BASE + _dt.timedelta(minutes=40)
    _turn(conn, windows.iso(t0 + _dt.timedelta(minutes=5)), 4000)  # inside [t0, t1]
    _snap(conn, windows.iso(t0), "session", 10.0, windows.iso(reset))
    _snap(conn, windows.iso(t1), "session", 30.0, windows.iso(reset))  # the true peak
    _snap(conn, windows.iso(t2), "session", 20.0, windows.iso(reset))  # trailing dip
    conn.commit()
    _set_last_ingest(conn, t2)

    caps = windows.cap_estimates(conn)
    # widest honest pair is 10% (t0) -> 30% (t1), not 10% -> 20% (t0 -> t2)
    from ccmetrics import costs
    expected_dtok = 4000 * costs._MREAD
    assert caps["session"]["cap_equiv"] == pytest.approx(expected_dtok / 0.20)
    assert caps["session"]["samples"] == 1


def test_cap_estimates_single_snapshot_falls_back_to_absolute_form(conn):
    end = BASE
    _turn(conn, windows.iso(end - _dt.timedelta(hours=1)), 3000)
    _snap(conn, windows.iso(end), "session", 10.0, None)
    conn.commit()
    _set_last_ingest(conn, end)

    caps = windows.cap_estimates(conn)
    assert caps["session"]["samples"] == 1
    assert caps["session"]["confidence"] == "single"
    assert caps["session"]["cap_equiv"] == pytest.approx(3000.0)


def test_cap_estimates_unknown_window_key_yields_no_cap(conn):
    _snap(conn, windows.iso(BASE), "mystery_window", 50.0, None)
    conn.commit()
    _set_last_ingest(conn, BASE)

    caps = windows.cap_estimates(conn)
    assert caps == {}


def test_cap_estimates_empty_store_returns_empty_dict_no_exception(conn):
    assert windows.cap_estimates(conn) == {}

    _set_last_ingest(conn, BASE)
    assert windows.cap_estimates(conn) == {}


# --- _pick: SESSION_KEYS/WEEKLY_KEYS resolution (PLAN-dash-v2 5) ------------

def test_pick_prefers_first_present_key():
    mapping = {"five_hour": {"used_pct": 10.0}, "session": {"used_pct": 20.0}}
    assert windows._pick(mapping, *windows.SESSION_KEYS) == {"used_pct": 20.0}

def test_pick_falls_back_to_legacy_name():
    mapping = {"five_hour": {"used_pct": 10.0}}
    assert windows._pick(mapping, *windows.SESSION_KEYS) == {"used_pct": 10.0}

def test_pick_returns_empty_dict_when_none_present():
    assert windows._pick({}, *windows.SESSION_KEYS) == {}
    assert windows._pick({"weekly_scoped_fable": {"used_pct": 99.0}}, *windows.WEEKLY_KEYS) == {}

def test_weekly_scoped_does_not_satisfy_weekly_lookup():
    """A scoped bar is one model's window, not account-wide -- it must never
    stand in for the weekly cap/percentage, cache-sourced key names or not."""
    mapping = {"weekly_scoped_fable": {"used_pct": 59.0, "cap_equiv": 12345.0}}
    assert windows._pick(mapping, *windows.WEEKLY_KEYS) == {}

    caps = {"weekly_scoped_fable": {"cap_equiv": 12345.0}}
    assert windows._pick(caps, *windows.WEEKLY_KEYS).get("cap_equiv") is None

def test_projection_ignores_weekly_scoped_snapshot(conn):
    """End-to-end: only a weekly_scoped_fable snapshot on the store -- the
    hero must not treat it as the account-wide weekly window."""
    _snap(conn, windows.iso(BASE), "weekly_scoped_fable", 59.0, None)
    conn.commit()
    _set_last_ingest(conn, BASE)

    hero = windows.projection(conn, blocks=[], caps={}, now=BASE)
    assert hero["used_pct"] is None
    assert hero["resets_at"] is None


# --- headroom labelling: the parsed cache label must reach the row (6.4f) ---

def test_headroom_scoped_key_falls_back_to_deunderscored_label(conn):
    """The store only ever holds a raw window_key; a scoped key's real label
    ("This week . Fable") used to come from the /usage cache's own parsed
    reading via `windows_payload`'s `cache_windows` lookup. That source is
    gone (session 2026-08-09, item 4: the cache is stale and dropped
    entirely) -- windows_payload no longer reads it, so a scoped row falls
    back to whatever plan.window_labels()/`_headroom_short` can derive from
    the raw key alone, same as any key `_headroom` has never seen labelled."""
    p = plan.usage_cache_path()
    p.write_text(json.dumps({
        "cachedUsageUtilization": {
            "fetchedAtMs": 1785477153001,
            "utilization": {
                "limits": [
                    {"kind": "weekly_scoped", "percent": 59, "resets_at": None,
                     "is_active": True,
                     "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
                ],
            },
        }
    }))
    plan.record_usage_cache(conn)
    conn.commit()
    windows.store.set_meta(conn, "last_ingest", windows.iso(BASE))
    conn.commit()

    payload = windows.windows_payload(conn, now=BASE)
    rows = {r["key"]: r for r in payload["headroom"]}
    assert "weekly_scoped_fable" in rows
    row = rows["weekly_scoped_fable"]
    assert row["label"] == "weekly scoped fable"
    assert row["short_label"] == "Week \u00b7 fable only"  # 6.4h H5: distinct per scoped model, key-derived now

def test_headroom_falls_back_to_window_labels_when_key_not_in_current_cache(conn):
    """A key the CURRENT cache read does not cover (e.g. a legacy hook key,
    or a stale snapshot after the cache moved on) still gets a real label,
    from plan.window_labels() -- not blank, not a raw key dump."""
    _snap(conn, windows.iso(BASE), "five_hour", 10.0, None)
    conn.commit()
    _set_last_ingest(conn, BASE)

    rows = {r["key"]: r for r in windows._headroom(conn, {}, BASE, cache_windows={})}
    assert rows["five_hour"]["label"] == "last 5 hours"
    assert rows["five_hour"]["short_label"] == "5-hour block"  # 6.4h H5


# --- falsify_stale_caps + early_hours (PLAN-dash-v2 6.4g #1 and #2) ---------

def test_falsify_stale_caps_drops_cap_a_finished_block_exceeds():
    caps = {
        "session": {"cap_equiv": 1000.0, "samples": 1, "confidence": "single"},
        "weekly_all": {"cap_equiv": 50000.0, "samples": 5, "confidence": "good"},
    }
    finished = {
        "start": windows.iso(BASE - _dt.timedelta(hours=6)),
        "end": windows.iso(BASE - _dt.timedelta(hours=1)),
        "equiv_tokens": 5000.0,  # far more than the session cap claims exists
    }
    out = windows.falsify_stale_caps(caps, [finished], BASE)
    assert "session" not in out  # falsified: a real block already beat the cap
    assert "weekly_all" in out  # untouched: no 7-day-scale block exists to check it against
    assert out["weekly_all"]["cap_equiv"] == 50000.0

def test_falsify_stale_caps_tolerates_a_modest_overage(conn):
    """PLAN-cap-and-chrome regression fix: a real block landing ~11% over a
    tightened, more accurate cap is ordinary dryness, not proof the estimate
    is broken -- it must not wipe the cap for every consumer downstream (dry
    counts, pct_of_cap, the heaviest ranking). Only a gross overage (the
    "estimate is nonsense" case this guard exists for) still falsifies."""
    caps = {"session": {"cap_equiv": 1000.0, "samples": 4, "confidence": "rough"}}
    finished = {
        "start": windows.iso(BASE - _dt.timedelta(hours=6)),
        "end": windows.iso(BASE - _dt.timedelta(hours=1)),
        "equiv_tokens": 1100.0,  # 10% over -- within the same tolerance as _headroom's H3
    }
    out = windows.falsify_stale_caps(caps, [finished], BASE)
    assert out["session"]["cap_equiv"] == 1000.0  # kept, not falsified


def test_falsify_stale_caps_keeps_cap_no_finished_block_exceeds():
    caps = {"session": {"cap_equiv": 100000.0, "samples": 3, "confidence": "rough"}}
    finished = {
        "start": windows.iso(BASE - _dt.timedelta(hours=6)),
        "end": windows.iso(BASE - _dt.timedelta(hours=1)),
        "equiv_tokens": 5000.0,
    }
    out = windows.falsify_stale_caps(caps, [finished], BASE)
    assert out["session"]["cap_equiv"] == 100000.0

def test_falsify_stale_caps_ignores_unfinished_blocks():
    caps = {"session": {"cap_equiv": 1000.0, "samples": 1, "confidence": "single"}}
    unfinished = {
        "start": windows.iso(BASE - _dt.timedelta(hours=1)),
        "end": windows.iso(BASE + _dt.timedelta(hours=4)),  # still in progress
        "equiv_tokens": 5000.0,
    }
    out = windows.falsify_stale_caps(caps, [unfinished], BASE)
    assert out["session"]["cap_equiv"] == 1000.0  # not finished -- can't falsify from it

def test_falsify_stale_caps_empty_inputs_no_exception():
    assert windows.falsify_stale_caps({}, [], BASE) == {}
    assert windows.falsify_stale_caps({"session": {"cap_equiv": 1.0}}, [], BASE) == {
        "session": {"cap_equiv": 1.0}
    }

def test_score_provisional_cap_never_claims_dry():
    cell = {"equiv_tokens": 999.0, "max_block_equiv": 999.0, "blocks": 1}
    windows._score(cell, 100.0, provisional=True)
    assert cell["dry"] is False
    assert cell["pct_of_cap"] == 999.0  # still fills/percentages -- just no dry claim

def test_dry_blocks_counts_regardless_of_cap_confidence():
    # PLAN-dash round2 1: `_dry_blocks` no longer takes a `provisional` flag --
    # a provisional cap is still a real number to check dryness against. The
    # page labels the count PROVISIONAL via `cap5_provisional`; this function
    # never withholds it as null.
    blocks = [{"local_date": "2026-08-01", "equiv_tokens": 100.0}]
    assert windows._dry_blocks(blocks, 100.0, "2026-08-01") == 1

def test_projection_early_hours_none_when_runs_out_after_reset(conn):
    """When the burn rate is slow enough that the week would run out AFTER
    its own reset, the week holds -- early_hours must be None, not a negative
    "late" figure that matches neither the stored value nor reality."""
    reset = BASE + _dt.timedelta(days=2)
    _snap(conn, windows.iso(BASE), "weekly_all", 10.0, windows.iso(reset))
    conn.commit()

    caps = {"weekly_all": {"cap_equiv": 1000000.0, "samples": 5, "confidence": "good"}}
    blocks = [{"start": windows.iso(BASE - _dt.timedelta(hours=1)), "equiv_tokens": 100.0}]
    hero = windows.projection(conn, blocks=blocks, caps=caps, now=BASE)
    assert hero["runs_out_at"] is not None  # a projection exists
    assert hero["early_hours"] is None  # but it is not "before the reset"

def test_projection_early_hours_positive_when_runs_out_before_reset(conn):
    reset = BASE + _dt.timedelta(hours=10)
    _snap(conn, windows.iso(BASE), "weekly_all", 10.0, windows.iso(reset))
    conn.commit()

    caps = {"weekly_all": {"cap_equiv": 1000.0, "samples": 5, "confidence": "good"}}
    blocks = [{"start": windows.iso(BASE - _dt.timedelta(hours=1)), "equiv_tokens": 100000.0}]
    hero = windows.projection(conn, blocks=blocks, caps=caps, now=BASE)
    assert hero["early_hours"] is not None
    assert hero["early_hours"] > 0


# --- RATE's dollar half: 24h fallback when no session is live --------------

def test_burn_usd_per_hour_prices_trailing_blocks_with_no_live_session():
    """No live session -> RATE's dollar half used to go blank even though the
    token half kept averaging the trailing 24h of blocks. It now prices that
    same window off each block's per_model equiv tokens, so both halves come
    from the same basis."""
    blocks = [{
        "start": windows.iso(BASE - _dt.timedelta(hours=1)),
        "equiv_tokens": 1000.0,
        "per_model": {"claude-sonnet-5": 1000.0},
    }]
    usd = windows._burn_usd_per_hour(blocks, None, now=BASE)
    assert usd is not None
    assert usd > 0

def test_burn_usd_per_hour_prefers_live_session_when_present():
    live_tiles = {"status": "live", "burn": {"floor_usd_per_hour": 9.5}}
    blocks = [{
        "start": windows.iso(BASE - _dt.timedelta(hours=1)),
        "equiv_tokens": 1000.0,
        "per_model": {"claude-sonnet-5": 1000.0},
    }]
    assert windows._burn_usd_per_hour(blocks, live_tiles, now=BASE) == 9.5

def test_burn_usd_per_hour_ignores_blocks_outside_the_trailing_24h():
    old_block = {
        "start": windows.iso(BASE - _dt.timedelta(hours=48)),
        "equiv_tokens": 1000.0,
        "per_model": {"claude-sonnet-5": 1000.0},
    }
    assert windows._burn_usd_per_hour([old_block], None, now=BASE) is None

def test_burn_usd_per_hour_none_when_nothing_can_be_priced():
    """An unrated model in every trailing block leaves nothing to price --
    None, not a silently wrong $0/hr."""
    blocks = [{
        "start": windows.iso(BASE - _dt.timedelta(hours=1)),
        "equiv_tokens": 1000.0,
        "per_model": {"some-unrated-model": 1000.0},
    }]
    assert windows._burn_usd_per_hour(blocks, None, now=BASE) is None

def test_burn_usd_per_hour_handles_no_blocks_without_exception():
    assert windows._burn_usd_per_hour(None, None, now=BASE) is None
    assert windows._burn_usd_per_hour([], None, now=BASE) is None


# --- H1: a merged cell's denominator must scale with its block count -------

def test_score_scales_denominator_by_merged_block_count():
    """PLAN-dash-v2 6.4h H1: a cell that merged 3 real blocks must be judged
    against 3 blocks' worth of cap, not one -- the actual cause of the "300%"
    week-grid bug (19 of 21 real week cells merged one block and read <=98%;
    the only three over 100% were exactly the three that merged more)."""
    cap5 = 10.0
    one_block = {"equiv_tokens": 9.0, "max_block_equiv": 9.0, "blocks": 1}
    three_blocks = {"equiv_tokens": 27.0, "max_block_equiv": 9.0, "blocks": 3}
    windows._score(one_block, cap5)
    windows._score(three_blocks, cap5)
    assert one_block["pct_of_cap"] == pytest.approx(90.0)
    # 3x the tokens over 3x the cap (3 blocks) is the SAME fill, not 300%
    assert three_blocks["pct_of_cap"] == pytest.approx(90.0)

def test_score_zero_blocks_has_no_percentage():
    cell = {"equiv_tokens": 0.0, "max_block_equiv": 0.0, "blocks": 0}
    windows._score(cell, 10.0)
    assert cell["pct_of_cap"] is None
    assert cell["dry"] is False

def test_score_dry_stays_per_single_block_not_scaled():
    """Dryness is judged per real 5-hour block, never per merged cell -- a
    3-block cell whose single fullest block hit the cap is dry; a 3-block
    cell that merely SUMS to the (correctly-scaled) cap, with no one block
    near it, must not be."""
    cap5 = 10.0
    one_ran_dry = {"equiv_tokens": 12.0, "max_block_equiv": 10.0, "blocks": 3}
    windows._score(one_ran_dry, cap5)
    assert one_ran_dry["dry"] is True

    none_ran_dry = {"equiv_tokens": 27.0, "max_block_equiv": 9.0, "blocks": 3}
    windows._score(none_ran_dry, cap5)
    assert none_ran_dry["dry"] is False


# --- PLAN-fill-and-clock: pct_of_week is a share of the WEEKLY cap ----------

def test_score_week_is_share_of_the_weekly_cap_not_scaled_by_blocks():
    """Unlike pct_of_cap, pct_of_week is never multiplied by `blocks` -- cap7
    is one week-long allowance, not one that grows just because a slot merged
    more than one real 5-hour block."""
    cap7 = 100.0
    one_block = {"equiv_tokens": 6.0, "blocks": 1}
    three_blocks = {"equiv_tokens": 6.0, "blocks": 3}
    windows._score_week(one_block, cap7)
    windows._score_week(three_blocks, cap7)
    assert one_block["pct_of_week"] == pytest.approx(6.0)
    assert three_blocks["pct_of_week"] == pytest.approx(6.0)


def test_score_week_edges_match_the_published_band_boundaries():
    """The live store's own distribution set WEEK_LVL_EDGES to [6, 12, 20]
    (see index.html); this pins the boundary VALUES pct_of_week actually
    produces at those exact edges, so the two stay in sync."""
    cap7 = 100.0
    for tokens, expected in ((6.0, 6.0), (12.0, 12.0), (20.0, 20.0), (23.07, 23.07)):
        cell = {"equiv_tokens": tokens, "blocks": 1}
        windows._score_week(cell, cap7)
        assert cell["pct_of_week"] == pytest.approx(expected)


def test_score_week_none_without_a_weekly_cap_or_blocks():
    no_cap = {"equiv_tokens": 6.0, "blocks": 1}
    windows._score_week(no_cap, None)
    assert no_cap["pct_of_week"] is None

    no_blocks = {"equiv_tokens": 0.0, "blocks": 0}
    windows._score_week(no_blocks, 100.0)
    assert no_blocks["pct_of_week"] is None


def test_score_week_never_touches_pct_of_cap_or_dry():
    """Rescaling the fill must not start asserting a window ran dry when it
    did not -- dryness stays `_score`'s claim about a real 5-hour block."""
    cap5, cap7 = 10.0, 100.0
    cell = {"equiv_tokens": 12.0, "max_block_equiv": 10.0, "blocks": 3}
    windows._score(cell, cap5)
    pct_of_cap_before, dry_before = cell["pct_of_cap"], cell["dry"]
    windows._score_week(cell, cap7)
    assert cell["pct_of_cap"] == pct_of_cap_before
    assert cell["dry"] == dry_before
    assert "pct_of_week" in cell


def test_windows_payload_horizon_and_week_carry_pct_of_week_when_weekly_cap_known(conn):
    """The 30-day strip's relative-shading fallback must stop firing once a
    real weekly cap exists (PLAN-fill-and-clock definition of done) -- the
    frontend's guard reads `pct_of_week`, so it must actually be populated on
    the payload's cells whenever a weekly cap is known, not left None the way
    the old 5-hour-only `_score` left it for a roomy cap. `cap_estimates` is
    stubbed here -- estimating a real cap from a delta pair is exercised
    elsewhere; this test is only about `windows_payload` wiring cap7 through
    to every week/horizon cell once one exists."""
    _turn(conn, windows.iso(BASE - _dt.timedelta(hours=1)), 20000)
    conn.commit()
    _set_last_ingest(conn, BASE)
    caps = {
        "session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "good"},
        "weekly_all": {"cap_equiv": 5000.0, "samples": 3, "confidence": "good"},
    }
    with mock.patch.object(windows, "cap_estimates", return_value=caps), \
         mock.patch.object(windows, "falsify_stale_caps", side_effect=lambda c, b, n: c):
        payload = windows.windows_payload(conn, now=BASE)
    assert payload["caps_known"] is True
    horizon_pcts = [c.get("pct_of_week") for c in payload["horizon"]]
    assert any(p is not None for p in horizon_pcts)
    week_cells = [c for row in payload["week"] for c in row["cells"] if c is not None]
    week_pcts = [c.get("pct_of_week") for c in week_cells]
    assert any(p is not None for p in week_pcts)

def test_windows_payload_dry_counts_are_real_numbers_under_provisional_cap5(conn):
    """PLAN-dash round2 1: a provisional (single-sample) cap5 must not blank
    out dry_count_week/month -- they are real counts now, just labelled
    PROVISIONAL via cap5_provisional, never withheld as null."""
    _turn(conn, windows.iso(BASE - _dt.timedelta(hours=1)), 1000)  # equiv 100.0
    conn.commit()
    _set_last_ingest(conn, BASE)
    caps = {"session": {"cap_equiv": 100.0, "samples": 1, "confidence": "single"}}
    with mock.patch.object(windows, "cap_estimates", return_value=caps), \
         mock.patch.object(windows, "falsify_stale_caps", side_effect=lambda c, b, n: c):
        payload = windows.windows_payload(conn, now=BASE)
    assert payload["cap5_provisional"] is True
    assert payload["dry_count_week"] == 1
    assert payload["dry_count_month"] == 1


# --- H3/H4/H6: headroom note basis, stale reset, reset time (6.4h) ---------

def test_headroom_stale_reset_goes_to_unknown_not_current(conn):
    """PLAN-dash-v2 6.4h H4: a reading whose own window already reset
    describes a window that no longer exists -- shown as unknown, not as
    live headroom."""
    past_reset = BASE - _dt.timedelta(hours=1)  # already elapsed
    _snap(conn, windows.iso(BASE), "session", 50.0, windows.iso(past_reset))
    conn.commit()
    _set_last_ingest(conn, BASE)

    caps = {"session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "rough"}}
    rows = {r["key"]: r for r in windows._headroom(conn, caps, BASE, cache_windows={})}
    row = rows["session"]
    assert row["used_pct"] is None
    assert row["left_pct"] is None
    assert row["cap_equiv"] is None
    assert "already reset" in row["note"]

def test_headroom_note_uses_reading_basis_not_seen_tokens(conn):
    """PLAN-dash-v2 6.4h H3: the note's token figure must be derived from the
    SAME reading the bar uses (cap * used_pct), not an independently counted
    "seen" total that can silently disagree with the bar."""
    reset = BASE + _dt.timedelta(hours=5)
    _snap(conn, windows.iso(BASE), "session", 50.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, BASE)
    caps = {"session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "rough"}}

    rows = {r["key"]: r for r in windows._headroom(conn, caps, BASE, cache_windows={})}
    row = rows["session"]
    assert row["window_equiv"] == pytest.approx(500.0)  # cap * 50% -- the reading's own basis
    assert row["left_equiv"] == pytest.approx(500.0)
    assert "500" in row["note"] or "500.0" in row["note"] or row["window_equiv"] == 500.0

def test_headroom_note_includes_reset_time_and_provenance(conn):
    """PLAN-dash-v2 6.4h H6: the note keeps its provenance AND the reset
    time, the way the mock's own note has both."""
    reset = BASE + _dt.timedelta(hours=5)
    _snap(conn, windows.iso(BASE), "session", 50.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, BASE)
    caps = {"session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "rough"}}

    rows = {r["key"]: r for r in windows._headroom(conn, caps, BASE, cache_windows={})}
    note = rows["session"]["note"]
    assert "resets" in note
    assert "reading" in note

def test_headroom_note_reset_includes_weekday_when_not_today(conn):
    """A weekly window resets days out, not tonight -- "resets 17:30" with
    no day reads as "later today". The weekday should show whenever the
    reset lands on a different local day than `now`.

    `_local` is pinned to the identity function so the assertion holds
    regardless of the machine's timezone -- only the UTC calendar dates
    of `now` (BASE) and `reset` are exercised, three days apart."""
    reset = BASE + _dt.timedelta(days=3, hours=5)
    _snap(conn, windows.iso(BASE), "session", 50.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, BASE)
    caps = {"session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "rough"}}

    with mock.patch.object(windows, "_local", side_effect=lambda d: d):
        rows = {r["key"]: r for r in windows._headroom(conn, caps, BASE, cache_windows={})}
    note = rows["session"]["note"]
    assert reset.strftime("%a") in note

def test_headroom_note_reset_bare_when_today(conn):
    """The reset stays bare (no weekday) when it lands on the same local
    day as `now` -- the day is already implied. `_local` pinned to the
    identity function for the same timezone-independence reason as above;
    BASE and reset are one hour apart, same UTC calendar day."""
    reset = BASE + _dt.timedelta(hours=1)
    _snap(conn, windows.iso(BASE), "session", 50.0, windows.iso(reset))
    conn.commit()
    _set_last_ingest(conn, BASE)
    caps = {"session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "rough"}}

    with mock.patch.object(windows, "_local", side_effect=lambda d: d):
        rows = {r["key"]: r for r in windows._headroom(conn, caps, BASE, cache_windows={})}
    note = rows["session"]["note"]
    assert reset.strftime("%a") not in note

def test_headroom_falsifies_row_when_seen_far_exceeds_reading(conn):
    """PLAN-dash-v2 6.4h H3: counted tokens well beyond what the reading
    implies is a falsification signal for THIS row, not a second number to
    print beside the bar."""
    # the window ends (resets) in 1h and spans 5h, so it started 4h ago --
    # "seen" covers [BASE-4h, BASE], well before `now`.
    reset = BASE + _dt.timedelta(hours=1)
    _snap(conn, windows.iso(BASE), "session", 10.0, windows.iso(reset))
    conn.commit()
    # cap=1000, used=10% -> reading implies 100 burnt; observed far more
    _turn(conn, windows.iso(BASE - _dt.timedelta(hours=1)), 5000)  # 500 equiv
    _set_last_ingest(conn, BASE)
    caps = {"session": {"cap_equiv": 1000.0, "samples": 3, "confidence": "rough"}}

    rows = {r["key"]: r for r in windows._headroom(conn, caps, BASE, cache_windows={})}
    row = rows["session"]
    assert row["cap_equiv"] is None  # falsified for this row
    assert row["window_equiv"] is None


# --- heaviest windows: ranked by the column the table actually prints -------


def _week_of_mixed_weights(conn, with_cap=True):
    """A week whose biggest cell by raw tokens is NOT its biggest by
    percentage: the 3-block cell holds the most tokens, but `_score` divides
    it by three caps, so single-block cells show a far higher share."""
    t0, t1 = BASE - _dt.timedelta(hours=2), BASE - _dt.timedelta(hours=1)
    _turn(conn, windows.iso(t0 + _dt.timedelta(minutes=10)), 20000)
    if with_cap:
        reset = windows.iso(BASE + _dt.timedelta(hours=3))
        _snap(conn, windows.iso(t0), "session", 10.0, reset)
        _snap(conn, windows.iso(t1), "session", 20.0, reset)
    for day in range(1, 7):
        for hour in (2, 7, 9, 13, 19, 22):
            at = BASE - _dt.timedelta(days=day) + _dt.timedelta(hours=hour - 12)
            _turn(conn, windows.iso(at), 1000 * hour)
    conn.commit()
    _set_last_ingest(conn, BASE)
    return windows.windows_payload(conn, now=BASE)


def test_heaviest_is_ranked_by_pct_of_cap_the_used_column_shows(conn):
    payload = _week_of_mixed_weights(conn)
    heaviest = payload["heaviest"]
    cells = [c for row in payload["week"] for c in row["cells"] if c is not None]

    assert payload["caps"]["session"]["cap_equiv"] == pytest.approx(20000.0)
    assert max(c["blocks"] for c in cells) >= 2  # the merged-cell case exists

    pcts = [h["pct_of_cap"] for h in heaviest]
    assert pcts == sorted(pcts, reverse=True)  # the visible column is in order
    assert pcts[0] == pytest.approx(max(c["pct_of_cap"] for c in cells))

    # and the old key really would have disagreed: the token-heaviest cell is
    # not the percentage-heaviest one
    fattest = max(cells, key=lambda c: c["equiv_tokens"])
    assert fattest["equiv_tokens"] > heaviest[0]["equiv_tokens"]


def test_heaviest_with_no_cap_sorts_on_tokens_and_never_compares_none(conn):
    """Without a cap every `pct_of_cap` is None -- which must not raise, and
    must not degrade to 'the eight most recent'. The table falls back to
    printing tokens there, so the ranking follows tokens too."""
    payload = _week_of_mixed_weights(conn, with_cap=False)
    heaviest = payload["heaviest"]

    assert payload["caps"] == {}
    assert all(h["pct_of_cap"] is None for h in heaviest)
    tokens = [h["equiv_tokens"] for h in heaviest]
    assert tokens == sorted(tokens, reverse=True)


# --- PLAN-window-identity: one window, one identity ------------------------

def test_collapse_windows_newest_wins_both_alias_orders():
    """Same identity, either tuple order: whichever alias's `ts` is newest
    wins, not whichever name sorts/appears first."""
    older = {"used_pct": 91.0, "ts": windows.iso(BASE), "resets_at": None}
    newer = {"used_pct": 95.0, "ts": windows.iso(BASE + _dt.timedelta(minutes=1)),
              "resets_at": None}

    a = windows.collapse_windows({"weekly_all": older, "seven_day": newer})
    assert a["weekly_all"]["used_pct"] == pytest.approx(95.0)
    assert a["weekly_all"]["raw_key"] == "seven_day"

    b = windows.collapse_windows({"seven_day": older, "weekly_all": newer})
    assert b["weekly_all"]["used_pct"] == pytest.approx(95.0)
    assert b["weekly_all"]["raw_key"] == "weekly_all"


def test_collapse_windows_missing_or_malformed_ts_never_wins_never_raises():
    good = {"used_pct": 50.0, "ts": windows.iso(BASE), "resets_at": None}
    missing_ts = {"used_pct": 99.0, "resets_at": None}
    malformed_ts = {"used_pct": 99.0, "ts": "not-a-timestamp", "resets_at": None}

    out = windows.collapse_windows({"session": good, "five_hour": missing_ts})
    assert out["session"]["used_pct"] == pytest.approx(50.0)

    out2 = windows.collapse_windows({"five_hour": malformed_ts, "session": good})
    assert out2["session"]["used_pct"] == pytest.approx(50.0)

    # both sides unparseable -- must not raise, and keeps a stable single entry
    out3 = windows.collapse_windows({"session": missing_ts, "five_hour": malformed_ts})
    assert len(out3) == 1


def test_collapse_windows_single_source_behaves_as_today():
    only = {"used_pct": 42.0, "ts": windows.iso(BASE), "resets_at": None}
    out = windows.collapse_windows({"session": only})
    assert out == {"session": {**only, "raw_key": "session"}}


def test_collapse_windows_scoped_identities_never_merge_across_models():
    fable = {"used_pct": 97.0, "ts": windows.iso(BASE), "resets_at": None}
    opus = {"used_pct": 40.0, "ts": windows.iso(BASE), "resets_at": None}
    out = windows.collapse_windows({"weekly_scoped_fable": fable, "weekly_scoped_opus": opus})
    assert set(out) == {"weekly_scoped_fable", "weekly_scoped_opus"}
    assert out["weekly_scoped_fable"]["used_pct"] == pytest.approx(97.0)
    assert out["weekly_scoped_opus"]["used_pct"] == pytest.approx(40.0)


def test_projection_hero_uses_newest_weekly_reading_regardless_of_feed_name(conn):
    """The hero must report the newest weekly reading, whichever feed named
    it -- not whichever name sorts first (PLAN-window-identity failure 1)."""
    _snap(conn, windows.iso(BASE - _dt.timedelta(hours=12)), "weekly_all", 91.0, None)
    _snap(conn, windows.iso(BASE), "seven_day", 95.0, None)
    conn.commit()

    hero = windows.projection(conn, blocks=[], caps={}, now=BASE)
    assert hero["used_pct"] == pytest.approx(95.0)


def test_headroom_row_count_collapses_both_name_sets_to_three(conn):
    """A store holding both the /usage-cache names and the statusline-feed
    names for the same three real windows renders exactly three headroom
    rows, not five (PLAN-window-identity failure 2)."""
    _snap(conn, windows.iso(BASE), "session", 28.0, windows.iso(BASE + _dt.timedelta(hours=1)))
    _snap(conn, windows.iso(BASE - _dt.timedelta(hours=12)), "five_hour", 5.0,
          windows.iso(BASE - _dt.timedelta(hours=7)))
    _snap(conn, windows.iso(BASE - _dt.timedelta(hours=12)), "weekly_all", 91.0, None)
    _snap(conn, windows.iso(BASE), "seven_day", 95.0, None)
    _snap(conn, windows.iso(BASE - _dt.timedelta(hours=12)), "weekly_scoped_fable", 97.0, None)
    conn.commit()
    _set_last_ingest(conn, BASE)

    rows = windows._headroom(conn, {}, BASE, cache_windows={})
    assert len(rows) == 3
    by_key = {r["key"] for r in rows}
    assert by_key == {"session", "seven_day", "weekly_scoped_fable"}  # newest raw key per identity


def test_cap_estimates_merges_samples_across_alias_names(conn):
    """The same real weekly window reported under both name sets is estimated
    once, from every reading of it -- not once per raw key name, and not
    dropped as two separate one-sample windows (PLAN-window-identity 3)."""
    t0 = BASE
    t1 = BASE + _dt.timedelta(days=10)
    _turn(conn, windows.iso(t0 - _dt.timedelta(hours=1)), 5000)
    _turn(conn, windows.iso(t1 - _dt.timedelta(hours=1)), 5000)
    _snap(conn, windows.iso(t0), "weekly_all", 10.0, None)  # cache name
    _snap(conn, windows.iso(t1), "seven_day", 10.0, None)   # feed name, same identity
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    assert "weekly_all" in caps
    assert "seven_day" not in caps  # one row per identity, not one per alias
    assert caps["weekly_all"]["samples"] == 2  # merged across both names


def test_cap_estimates_reset_tolerance_accepts_one_second_jitter(conn):
    """The two feeds can report the same rollover a second apart -- the
    delta-form estimator must still run across that jitter."""
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    reset_a = BASE + _dt.timedelta(hours=5)
    reset_b = reset_a + _dt.timedelta(seconds=1)  # sub-second/1s jitter, same rollover
    _turn(conn, windows.iso(t0 + _dt.timedelta(minutes=30)), 2000)
    _snap(conn, windows.iso(t0), "session", 10.0, windows.iso(reset_a))
    _snap(conn, windows.iso(t1), "five_hour", 20.0, windows.iso(reset_b))
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    assert caps["session"]["cap_equiv"] == pytest.approx(2000.0)  # delta form ran
    assert caps["session"]["samples"] == 1


def test_cap_estimates_reset_tolerance_still_rejects_a_real_rollover(conn):
    """A real rollover moves `resets_at` by a whole period, never a second --
    the tolerance must not swallow that too."""
    t0 = BASE
    t1 = BASE + _dt.timedelta(hours=1)
    reset0 = BASE + _dt.timedelta(hours=5)
    reset1 = reset0 + _dt.timedelta(hours=5)  # a whole period later -- an actual rollover
    _turn(conn, windows.iso(t0 - _dt.timedelta(minutes=30)), 5000)
    _turn(conn, windows.iso(t1 - _dt.timedelta(minutes=30)), 5000)
    _snap(conn, windows.iso(t0), "session", 20.0, windows.iso(reset0))
    _snap(conn, windows.iso(t1), "session", 30.0, windows.iso(reset1))
    conn.commit()
    _set_last_ingest(conn, t1)

    caps = windows.cap_estimates(conn)
    # pair rejected as a straddled reset -- falls back to the absolute form
    # per-reading (2 samples) instead of one delta sample
    assert caps["session"]["samples"] == 2
