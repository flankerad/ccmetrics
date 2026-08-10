"""Five-hour window mechanics for the dash (PLAN-redesign.md §1).

Everything the quota panels need, as pure functions over the existing store:
5-hour block bucketing, cap estimation from the statusline hook, and the weekly
projection the hero prints. Zero new dependencies.

Two rules run through the whole module.

* **Blocks step and are stored in UTC.** `day` / `hour` / `local_start` are
  display-only conversions to this machine's local clock, computed at read time
  (Decision 6). For anyone west of UTC on a late block this will not agree,
  day-boundary-wise, with the "Value absorbed" panel, which keys off the
  UTC-dated `daily` rollup the rest of the dash already uses. That mismatch is
  accepted on purpose: deriving day boundaries two different ways is worse.

* **A cap is never invented.** Plan limits live on Anthropic's servers and
  appear in no local file. Without the statusline hook there is no cap, and
  without a cap there is no percentage, no "ran dry" and no "runs out at" —
  every one of those is cap-relative by definition. `caps == {}` is a
  first-class state, not a missing number to be filled in with a plausible one.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from bisect import bisect_left, bisect_right
from statistics import median

from . import constants, costs, store
from . import plan as plan_mod

BLOCK_HOURS = 5
BLOCK = _dt.timedelta(hours=BLOCK_HOURS)
TURN_DAYS = constants.value(constants.RETENTION["turn_days"])

# Below this the cap division (tokens / used_pct) turns one rounding step in
# used_pct into a wildly wrong cap, so the sample is dropped rather than damped.
MIN_USED_PCT = 5.0

# "Ran dry" means the window actually reached its cap. Only ever evaluated
# against a KNOWN cap, and only ever per real 5-hour block (see _score).
DRY_PCT = 95.0

WEEK_DAYS = 7
SLOTS = 4
HEAVIEST = 8
HORIZON_DAYS = 30
HORIZON_HALVES = 2  # 30 days x 2 = the mock's 60-cell strip; see horizon()

SLOT_LABELS = ("morning", "afternoon", "evening", "late")
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

SETUP_CMD = "ccmetrics statusline --setup"

# How long each window the feed reports actually is. A key whose length we do
# not know gets no cap estimate rather than a guessed one.
WINDOW_HOURS = {"five_hour": 5.0, "seven_day": 168.0}


# --- time -------------------------------------------------------------------
#
# The store writes three timestamp shapes: turns and `last_ingest` carry
# milliseconds ("...T10:00:00.000Z"), plan snapshots do not ("...T10:00:00Z").
# They do NOT compare correctly as strings inside the same second ('Z' sorts
# after '.'), so every comparison that matters happens on parsed datetimes;
# SQL only ever gets a millisecond-shaped lower bound, which sorts before any
# same-second row instead of after it.


def parse_iso(ts):
    """A UTC-aware datetime, or None. Accepts both stored shapes."""
    if not isinstance(ts, str) or not ts.strip():
        return None
    t = ts.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def iso(d):
    """The store's second-resolution UTC shape, or None."""
    if d is None:
        return None
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bound(d):
    """A SQL lower bound that sorts before any row in the same second."""
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _local(d):
    return d.astimezone()


def slot(hour: int) -> int:
    """Display bucket for a block's LOCAL hour — morning/afternoon/evening/late.

    Computed, never stored: it is a column in a fixed 4-wide grid, not a
    property of the block.
    """
    return 0 if hour < 12 else 1 if hour < 17 else 2 if hour < 22 else 3


def window_span(key: str):
    """(hours, model substring) for a feed window key, or (None, None).

    `session` / `weekly_all` / `weekly_scoped_<model>` are the /usage-cache
    keys (PLAN-dash-v2 4); `five_hour` / `seven_day` are the legacy
    statusline-hook keys. `weekly_scoped_<model>` is the weekly window as it
    applies to one model alone, so the tokens paired with its percentage must
    be that model's tokens; pairing the all-model total against a scoped
    percentage inflates the cap silently.
    """
    if key == "session":
        return 5.0, None
    if key == "weekly_all":
        return 168.0, None
    if key.startswith(plan_mod.SCOPED_PREFIX):
        return 168.0, key[len(plan_mod.SCOPED_PREFIX):]
    for base, hours in WINDOW_HOURS.items():
        if key == base:
            return hours, None
        if key.startswith(base + "_"):
            return hours, key[len(base) + 1:]
    return None, None


def window_order(key: str):
    """plan.WINDOW_ORDER order, unknown keys last, stable by name."""
    order = plan_mod.WINDOW_ORDER
    return (order.index(key) if key in order else len(order), key)


def identity_of(key: str) -> str | None:
    """The stable window identity a feed key names, or None for an unknown key.

    Two name sets can name the same real window (PLAN-window-identity):
    `session`/`weekly_all`/`weekly_scoped_<model>` from the /usage cache and
    `five_hour`/`seven_day` from the legacy statusline-hook feed. This is the
    only place that knows those pairs are aliases of one another -- built on
    `window_span()` (hours, model-part) rather than a second hand-maintained
    table that can drift from it. The canonical identity string for a scoped
    window keeps its model (`weekly_scoped_fable` and `weekly_scoped_opus`
    are genuinely different limits and must never collapse together); for
    the two account-wide windows it is the /usage-cache's own name, since
    that is the name set the cache's per-window label/model data is keyed by
    downstream.
    """
    hours, part = window_span(key)
    if hours is None:
        return None
    if part:
        return plan_mod.SCOPED_PREFIX + part
    if hours == WINDOW_HOURS["five_hour"]:
        return "session"
    if hours == WINDOW_HOURS["seven_day"]:
        return "weekly_all"
    return None


# The raw key names either feed can write for an account-wide window, in
# preference order. Derived from `identity_of` rather than hand-maintained,
# so a third alias only ever needs adding once, to `window_span`.
_KNOWN_RAW_KEYS = ("session", "five_hour", "weekly_all", "seven_day")
SESSION_KEYS = tuple(k for k in _KNOWN_RAW_KEYS if identity_of(k) == "session")
WEEKLY_KEYS = tuple(k for k in _KNOWN_RAW_KEYS if identity_of(k) == "weekly_all")


def _pick(mapping: dict, *keys) -> dict:
    """The first of `keys` present (truthy) in `mapping`, or {} when none are."""
    for k in keys:
        v = mapping.get(k)
        if v:
            return v
    return {}


def _newer(a: dict, b: dict) -> bool:
    """True when reading `a` should replace `b` as an identity's representative.

    A missing or unparseable `ts` never beats one with a real timestamp, and
    this never raises on the comparison -- two unparseable stamps just keep
    whichever arrived first. A tie (equal parsed `ts`, plausible since the
    store's `ts` is second-resolution and two feeds can round to the same
    second) also keeps whichever arrived first -- `collapse_windows` visits
    keys in a fixed `window_order`, not raw dict/SQL order, so "first" is the
    same key on every reload rather than however SQLite happened to return
    the join that time.
    """
    ta, tb = parse_iso(a.get("ts")), parse_iso(b.get("ts"))
    if ta is None:
        return False
    if tb is None:
        return True
    return ta > tb


def collapse_windows(mapping: dict) -> dict:
    """{raw_key: {...}} -> {identity: {..., "raw_key": raw_key}}, newest wins.

    Both feeds can be live for the same window at once (PLAN-window-identity):
    the /usage cache only moves when someone opens /usage, the statusline
    feed writes on every redraw, so the two name sets drift out of sync with
    each other rather than staying in lockstep. Collapsing on read -- never
    rewriting the stored rows, which stay evidence of what each feed said --
    means every consumer sees one entry per real window, holding whichever
    alias's reading is newest by parsed `ts`. The winning raw key travels
    with the entry as `raw_key`, so labels, the source note and `--check`
    can still say which feed the figure came from. Keys are visited in
    `window_order`, not dict/SQL order, so an exact-`ts` tie between two
    aliases resolves the same way on every reload (see `_newer`).
    """
    out: dict[str, dict] = {}
    for key in sorted(mapping, key=window_order):
        v = mapping[key]
        ident = identity_of(key)
        if ident is None:
            continue
        cur = out.get(ident)
        if cur is None or _newer(v, cur):
            merged = dict(v)
            merged["raw_key"] = key
            out[ident] = merged
    return out


# --- 1.1 block bucketing ----------------------------------------------------


def _leader(per_model):
    """The model with the most equivalent tokens; None for an empty mix.

    Iterating the names in sorted order makes a tie resolve the same way on
    every reload instead of following dict insertion order.
    """
    if not per_model:
        return None
    return max(sorted(per_model), key=lambda m: per_model[m])


def _turn_rows(conn, project, since):
    sql = ("SELECT ts, model, cw5m, cw1h, cread, out_bytes FROM turns "
           "WHERE ts IS NOT NULL AND ts >= ?")
    args = [_bound(since)]
    if project:
        sql += " AND project = ?"
        args.append(project)
    return conn.execute(sql + " ORDER BY ts", args).fetchall()


def _anchor(conn, rows, now):
    """(anchor instant in UTC, "hook" | "midnight").

    The live 5-hour window's own reset instant is the right anchor when we have
    it: the blocks then line up with the boundaries Anthropic is counting
    against. With no hook, fall back to UTC midnight of the oldest day in
    range — arbitrary, but stable between reloads, which matters more than
    being clever.
    """
    resets = _pick(collapse_windows(store.latest_plan_windows(conn)), *SESSION_KEYS).get("resets_at")
    d = parse_iso(resets)
    if d is not None:
        return d, "hook"
    oldest = parse_iso(rows[0]["ts"]) if rows else None
    base = oldest or now
    return base.replace(hour=0, minute=0, second=0, microsecond=0), "midnight"


def _finish(acc, start):
    ls = _local(start)
    return {
        "start": iso(start),
        "end": iso(start + BLOCK),
        "local_start": ls.strftime("%Y-%m-%dT%H:%M:%S"),
        "local_date": ls.date().isoformat(),
        "time": ls.strftime("%H:%M"),
        "day": DAY_NAMES[ls.weekday()],
        "hour": ls.hour,
        "slot": slot(ls.hour),
        "equiv_tokens": acc["equiv_tokens"],
        "turns": acc["turns"],
        "out_bytes": acc["out_bytes"],
        "per_model": acc["per_model"],
        "leader": _leader(acc["per_model"]),
    }


def _bucket(conn, project=None, days=TURN_DAYS, now=None):
    """(blocks, anchor kind, anchor instant). Empty blocks are omitted."""
    now = now or _now()
    rows = _turn_rows(conn, project, now - _dt.timedelta(days=days))
    anchor, kind = _anchor(conn, rows, now)
    step = BLOCK_HOURS * 3600.0
    acc: dict[int, dict] = {}
    for r in rows:
        t = parse_iso(r["ts"])
        if t is None:
            continue
        n = int((t - anchor).total_seconds() // step)
        b = acc.get(n)
        if b is None:
            b = acc[n] = {"equiv_tokens": 0.0, "turns": 0, "out_bytes": 0, "per_model": {}}
        equiv = costs.billable_input_equivalent(
            r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0
        )
        b["equiv_tokens"] += equiv
        b["turns"] += 1
        b["out_bytes"] += r["out_bytes"] or 0
        m = r["model"] or "unknown"
        b["per_model"][m] = b["per_model"].get(m, 0.0) + equiv
    blocks = [_finish(acc[n], anchor + _dt.timedelta(seconds=step * n)) for n in sorted(acc)]
    return blocks, kind, anchor


def five_hour_blocks(conn, project=None, days=TURN_DAYS, now=None) -> list[dict]:
    """Turns bucketed into 5-hour blocks, stepped and stored in UTC."""
    return _bucket(conn, project, days, now)[0]


def _merge(blocks):
    """Sum a slot's blocks into one display cell, or None when the slot is empty.

    24 hours does not divide by 5, so a day holds 4 or 5 real blocks and at
    least one fixed time-of-day slot takes two of them on most days. The second
    one is summed in, never dropped — `max_block_equiv` is kept so dryness can
    still be judged per real window rather than per merged cell.
    """
    if not blocks:
        return None
    per_model: dict[str, float] = {}
    for b in blocks:
        for m, v in b["per_model"].items():
            per_model[m] = per_model.get(m, 0.0) + v
    first = blocks[0]
    return {
        "equiv_tokens": sum(b["equiv_tokens"] for b in blocks),
        "max_block_equiv": max(b["equiv_tokens"] for b in blocks),
        "turns": sum(b["turns"] for b in blocks),
        "out_bytes": sum(b["out_bytes"] for b in blocks),
        "per_model": per_model,
        "leader": _leader(per_model),
        "blocks": len(blocks),
        "start": first["start"],
        "time": first["time"],
        "hour": first["hour"],
        "slot": first["slot"],
        "day": first["day"],
        "local_date": first["local_date"],
    }


def _by_local_date(blocks):
    out: dict[str, list] = {}
    for b in blocks:
        out.setdefault(b["local_date"], []).append(b)
    return out


def week_grid(blocks, now=None, resets_at=None) -> list[dict]:
    """7 LOCAL calendar days, 4 merged slots each, anchored to the weekly
    reset (Bug 3, PLAN-fill-and-clock's own hero fuse convention).

    The weekly cap's window runs reset-minus-7-days to reset (see the hero
    fuse's day row, index.html ~line 315), so the grid's dates are pinned the
    same way: day 0 is the most recent occurrence of `resets_at`'s weekday,
    day 6 is the day before the NEXT reset. That is "this week" as the cap
    actually counts it, not just "whatever 7 days ended today" -- a rolling
    end-on-today window drifts a different 7 dates onto the same labels every
    day and, worse, does not agree with the hero fuse directly above it.

    No `resets_at` (hook never ran / no weekly reading yet) falls back to the
    last 7 local days ending today -- still real, still ordered, just without
    a reset instant to anchor the row to.
    """
    reset_dt = parse_iso(resets_at) if resets_at else None
    if reset_dt is not None:
        start = _local(reset_dt).date() - _dt.timedelta(days=WEEK_DAYS)
    else:
        start = _local(now or _now()).date() - _dt.timedelta(days=WEEK_DAYS - 1)
    index = _by_local_date(blocks)
    rows = []
    for i in range(WEEK_DAYS):
        d = start + _dt.timedelta(days=i)
        mine = index.get(d.isoformat(), [])
        rows.append({
            "day": DAY_NAMES[d.weekday()],
            "date": d.isoformat(),
            "blocks": len(mine),
            "cells": [_merge([b for b in mine if b["slot"] == s]) for s in range(SLOTS)],
        })
    return rows


def horizon_cells(blocks, now=None, days=HORIZON_DAYS) -> list[dict]:
    """One cell per half-day for `days` local days — 60 cells over 30 days.

    Plan addendum (§1.4, "count the actual number of cells"): the mock renders
    60 cells under a panel titled "30 days", but its own cells are single
    5-hour blocks, and 60 of those is ~12.5 days. Keeping BOTH the 60-cell
    shape and the 30-day span means two cells a day, each merging that
    half-day's blocks by the same merge-not-drop rule as the week grid. An
    un-aggregated block list would have shipped a panel whose title was false.
    """
    today = _local(now or _now()).date()
    index = _by_local_date(blocks)
    out = []
    for i in range(days - 1, -1, -1):
        d = today - _dt.timedelta(days=i)
        mine = index.get(d.isoformat(), [])
        for half in range(HORIZON_HALVES):
            part = [b for b in mine if (0 if b["hour"] < 12 else 1) == half]
            cell = _merge(part) or {
                "equiv_tokens": 0.0, "max_block_equiv": 0.0, "turns": 0,
                "out_bytes": 0, "per_model": {}, "leader": None, "blocks": 0,
            }
            cell.update({
                "date": d.isoformat(),
                "half": half,
                "label": f"{d:%b} {d.day} · " + ("morning" if half == 0 else "afternoon"),
                "evening": half == 1,
            })
            out.append(cell)
    return out


# --- 1.2 cap estimation -----------------------------------------------------


# The two feeds can report the same reset instant a second apart (observed:
# 17:29:59 vs 17:30:00), and the /usage cache alone already jitters
# sub-second between reads. Grouping samples by identity (below) means the
# delta estimator now sees BOTH feeds' readings in one series, so without
# some tolerance this jitter would make the reset-straddle check reject
# nearly every pair. A window that has actually rolled over moves its
# `resets_at` by a whole period -- never by a second, the shortest real
# period being the 5-hour block's 18000s -- so a few seconds of slack can
# never be mistaken for a real rollover.
RESET_JITTER_TOLERANCE_SECONDS = 5.0


def _same_reset(a, b) -> bool:
    """True when two `resets_at` strings name the same rollover, within
    RESET_JITTER_TOLERANCE_SECONDS. Falls back to plain equality (so two
    unparseable-but-identical strings, or two Nones, still count as the same)
    when either side does not parse.
    """
    da, db = parse_iso(a), parse_iso(b)
    if da is None or db is None:
        return a == b
    return abs((da - db).total_seconds()) <= RESET_JITTER_TOLERANCE_SECONDS


def _reset_runs(rows):
    """Split one window's time-sorted (end, pct, resets_at, hours, part) rows
    into runs that never straddle a reset (PLAN-cap-and-chrome Part 1).

    A "run" is one unbroken stretch of the same rollover instant -- the same
    real window filling up. Consecutive rows join a run while `_same_reset`
    holds between them; a rollover starts a new run.
    """
    runs = [[rows[0]]]
    for prev, cur in zip(rows, rows[1:]):
        if _same_reset(prev[2], cur[2]):
            runs[-1].append(cur)
        else:
            runs.append([cur])
    return runs


def _widest_pair(run):
    """The most accurate delta sample a run can give: the widest percentage
    spread inside it, not the narrowest (PLAN-cap-and-chrome Part 1).

    A status-line-fed store now holds readings seconds apart, so adjacent
    percentages are almost always identical -- pairing NEIGHBOURS starves the
    estimator (every pair rejected below the rounding floor, one reading of
    187 survives). Within one unbroken run the percentage only climbs, so the
    reading with the lowest percent and the one with the highest bound the
    widest, least-rounding-sensitive interval the run can offer; every
    reading between them is noise around that same line, not separate
    evidence. Returns (lo, hi) rows, or None when the run cannot give an
    honest rising pair (the highest percent does not come strictly after the
    lowest in time -- a real decrease, not just jitter, or a run of one).
    """
    lo = min(run, key=lambda r: (r[1], r[0]))
    hi = max(run, key=lambda r: (r[1], r[0]))
    if hi[0] <= lo[0]:  # highest percent is not strictly later -- no real rise
        return None
    return lo, hi


def cap_estimates(conn, now=None) -> dict:
    """{identity: {cap_equiv, samples, confidence}} -- {} when unknowable.

    Preferred estimator: widest-pair delta. Within one unbroken run (same
    reset instant, PLAN-cap-and-chrome Part 1), take the WIDEST percentage
    spread the run offers -- not adjacent readings -- and

        cap_equiv = delta(equiv_tokens between the two ts) / (delta(percent) / 100)

    which cancels out however much of the window had already elapsed before
    the first reading, instead of the absolute form's assumption that the
    window started at zero. Pairing the widest spread instead of neighbours
    matters once a run holds many readings seconds apart (a status-line-fed
    store, not the old sparse /usage-only feed): adjacent percentages are
    then almost always identical, so pairing neighbours rejects nearly every
    pair below the rounding floor and starves the estimator down to one
    sample from hundreds of readings. The widest pair is also the least
    rounding-sensitive estimate the run can give -- one sample per run, not
    one per adjacent pair, so a dense run contributes exactly the evidence it
    can support instead of counterfeit extra samples. A run's pair is
    rejected when the highest percent is not strictly later than the lowest
    (a real decrease, not jitter), when delta(percent) is below 2 (rounding
    dominates below that), or when either snapshot is newer than
    meta['last_ingest'] (the hook/cache fires live while ingest runs on
    demand, so a too-new snapshot is paired against a token sum that has not
    caught up). Reset-straddling is refused by construction: a run never
    crosses a reset (see `_reset_runs`).

    Fallback: the absolute form, tokens_in_window_at_ts / (used_pct / 100),
    used only for a window with no usable pair -- same two filters as
    before (ts vs last_ingest, used_pct floor).

    Either way: the median across all surviving samples, so one bad pair or
    reading cannot drag the answer. Samples are grouped by WINDOW IDENTITY,
    not raw feed key (PLAN-window-identity 3), so the same real limit is
    estimated once from every reading of it regardless of which feed named
    it. `samples` now counts independent WIDEST-PAIR estimates -- one per
    unbroken reset run -- never raw readings and never adjacent pairs
    (PLAN-cap-and-chrome Part 1): a single ongoing window that has not yet
    reset can only ever give ONE such estimate no matter how many readings
    arrived, because every intermediate reading sits on the same line the
    two extremes already describe. "single"/"rough" confidence here means
    "one (or a few) windows observed so far", not "not much data" -- the
    dense per-run reading count backing that one estimate can be, and often
    is, in the hundreds. Caps are account-wide, so this never takes a
    project scope: dividing an account-wide percentage by one project's
    tokens would invent a much smaller cap.
    """
    last_ingest = parse_iso(store.get_meta(conn, "last_ingest"))
    if last_ingest is None:
        return {}
    try:
        snaps = conn.execute(
            "SELECT ts, window_key, used_pct, resets_at FROM plan_snapshots "
            "WHERE used_pct IS NOT NULL ORDER BY window_key, ts"
        ).fetchall()
    except sqlite3.OperationalError:  # store predates the table
        return {}
    if not snaps:
        return {}

    by_window: dict[str, list] = {}
    for s in snaps:
        end = parse_iso(s["ts"])
        stale = end is None or last_ingest < end
        if stale:
            continue
        ident = identity_of(s["window_key"])
        if ident is None:  # window of unknown length -> no cap, no guess
            continue
        hours, part = window_span(s["window_key"])
        by_window.setdefault(ident, []).append(
            (end, float(s["used_pct"]), s["resets_at"], hours, part)
        )
    if not by_window:
        return {}

    # Grouping by identity can interleave rows from different raw keys (the
    # SQL's own ORDER BY window_key, ts no longer guarantees ts order once
    # merged), so each group is re-sorted before pairing.
    for rows in by_window.values():
        rows.sort(key=lambda r: r[0])

    oldest = min(
        row[0] - _dt.timedelta(hours=row[3])
        for rows in by_window.values() for row in rows
    )
    parts = {row[4] for rows in by_window.values() for row in rows}
    index = _turn_index(conn, oldest, parts)

    samples: dict[str, list] = {}
    for key, rows in by_window.items():
        part = rows[0][4]
        stamps, cum = index[part]

        # --- preferred: widest pair per unbroken reset run ---
        delta_vals = []
        for run in _reset_runs(rows):
            pair = _widest_pair(run)
            if pair is None:
                continue  # run of one, or highest percent not strictly later
            (end0, pct0, _r0, _h0, _p0), (end1, pct1, _r1, _h1, _p1) = pair
            dpct = pct1 - pct0
            if dpct < 2:  # covers "below the rounding floor" and "no real rise"
                continue
            tok0 = cum[bisect_right(stamps, end0)]
            tok1 = cum[bisect_right(stamps, end1)]
            dtok = tok1 - tok0
            if dtok <= 0:
                continue
            delta_vals.append(dtok / (dpct / 100.0))
        if delta_vals:
            samples[key] = delta_vals
            continue

        # --- fallback: absolute form ---
        abs_vals = []
        for end, pct, _reset, hours, _part in rows:
            if pct < MIN_USED_PCT:
                continue
            start = end - _dt.timedelta(hours=hours)
            total = cum[bisect_right(stamps, end)] - cum[bisect_left(stamps, start)]
            if total <= 0:
                continue
            abs_vals.append(total / (pct / 100.0))
        if abs_vals:
            samples[key] = abs_vals

    out = {}
    for key, vals in samples.items():
        n = len(vals)
        out[key] = {
            "cap_equiv": median(vals),
            "samples": n,
            "confidence": "good" if n >= 5 else "rough" if n >= 2 else "single",
        }
    return out


def falsify_stale_caps(caps: dict, blocks: list, now) -> dict:
    """Drop a cap that a completed window in range has already beaten by more
    than a small margin (PLAN-dash-v2 6.4g #1): a real observed block far
    bigger than the estimated cap proves the estimate wrong -- most often a
    single stale low-percentage reading (7% moves the cap roughly a seventh
    of its own value per rounding step). The falsified cap is discarded
    outright, never clamped to 100%: clamping would hide a broken
    denominator, and printing 338% asserts something impossible either way.

    The same ~20% tolerance as the `_headroom` H3 guard (`seen > burnt * 1.2
    + 1.0`) is applied here too (PLAN-cap-and-chrome regression fix): a
    tightened, more accurate cap estimate (e.g. from the widest-pair
    estimator) can be beaten by a modest, ordinary margin by a real block
    that simply ran close to the edge -- that is dryness, not a broken
    estimate, and zero-tolerance falsification was wiping the cap for every
    consumer of it (dry counts, `pct_of_cap`, the heaviest ranking) on a
    ~11% overage that was never the "estimate is nonsense" case this guard
    exists for.

    Only checked at the 5-hour block scale, the only one raw blocks exist at
    -- there is no comparably-scoped raw window to falsify a weekly cap
    against, so weekly-scale caps pass through unchecked.
    """
    if not caps:
        return caps
    out = dict(caps)
    finished = [b for b in blocks if (parse_iso(b["end"]) or now) <= now]
    if not finished:
        return out
    worst = max(b["equiv_tokens"] for b in finished)
    for key in list(out.keys()):
        hours, _part = window_span(key)
        if hours is None or abs(hours - BLOCK_HOURS) > 0.01:
            continue  # not a 5-hour-scale cap -- nothing comparable to check it against
        cap = out[key].get("cap_equiv")
        if cap and worst > cap * 1.2 + 1.0:
            del out[key]
    return out


def _turn_index(conn, oldest, parts):
    """{model-part: (stamps, cumulative equiv-tokens)} from `oldest` onward.

    `cumulative[i]` is the running total of the first `i` turns kept for that
    part; `cumulative[bisect_right(stamps, t)]` is the total at-or-before `t`.
    One prefix-sum index per model scope (usually just "everything"), so a
    month of snapshots against a month of turns stays a log-time lookup
    instead of a quadratic scan.
    """
    rows = conn.execute(
        "SELECT ts, model, cw5m, cw1h, cread FROM turns "
        "WHERE ts IS NOT NULL AND ts >= ? ORDER BY ts",
        (_bound(oldest),),
    ).fetchall()
    turns = []
    for r in rows:
        t = parse_iso(r["ts"])
        if t is None:
            continue
        turns.append((
            t,
            (r["model"] or "").lower(),
            costs.billable_input_equivalent(r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0),
        ))
    index: dict = {}
    for part in parts:
        stamps, cum, run = [], [0.0], 0.0
        for t, model, equiv in turns:
            if part and part not in model:
                continue
            run += equiv
            stamps.append(t)
            cum.append(run)
        index[part] = (stamps, cum)
    return index

def _equiv_in(conn, start, end, part):
    """Equivalent tokens ingested in [start, end], optionally one model family.

    Account-wide on purpose: it sits beside a percentage the hook reports for
    the whole account.
    """
    total = 0.0
    rows = conn.execute(
        "SELECT ts, model, cw5m, cw1h, cread FROM turns WHERE ts IS NOT NULL AND ts >= ?",
        (_bound(start),),
    ).fetchall()
    for r in rows:
        t = parse_iso(r["ts"])
        if t is None or t > end:
            continue
        if part and part not in (r["model"] or "").lower():
            continue
        total += costs.billable_input_equivalent(
            r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0
        )
    return total


# --- 1.3 projection ---------------------------------------------------------


def burn_equiv_per_hour(blocks, now=None, live_tiles=None) -> float:
    """Equivalent tokens per ELAPSED hour over the trailing 24 hours.

    The divisor is 24 flat — idle hours included. Averaging over only the
    non-empty blocks shrinks the divisor for every hour spent asleep without
    shrinking the numerator, so it systematically overstates burn, and
    runs-out time, early-hours and "% of a block per hour" all inherit that
    bias. A live session reports its own trailing burn, which is fresher than
    anything the ingested blocks can say, so it wins when present.
    """
    if live_tiles and live_tiles.get("status") == "live":
        v = (live_tiles.get("burn") or {}).get("equiv_tokens_per_hour")
        if v:
            return float(v)
    now = now or _now()
    cutoff = now - _dt.timedelta(hours=24)
    total = 0.0
    for b in blocks:
        start = parse_iso(b["start"])
        if start is not None and start >= cutoff:
            total += b["equiv_tokens"]
    return total / 24.0


def _burn_usd_per_hour(blocks, live_tiles, now=None):
    """Live-first, same trailing-24h fallback as `burn_equiv_per_hour`'s token
    half, so RATE never shows one real figure next to a blank one.

    A live session's floor $/hr is exact and wins outright. With no live
    session, `turns` carries no dollar column to read a figure straight off,
    so this prices the identical trailing-24h blocks the token average sums:
    each block's `per_model` equivalent tokens times that model's own input
    rate (`costs.py`'s floor arithmetic -- the block's own start time, so a
    rate change mid-window is honoured, not a flat multiplier invented here).
    None only when nothing in the window can be priced (e.g. every model in
    it is unrated).
    """
    if live_tiles and live_tiles.get("status") == "live":
        v = (live_tiles.get("burn") or {}).get("floor_usd_per_hour")
        if v is not None:
            return float(v)
    now = now or _now()
    cutoff = now - _dt.timedelta(hours=24)
    total = 0.0
    priced = False
    for b in (blocks or []):
        start = parse_iso(b["start"])
        if start is None or start < cutoff:
            continue
        for model, equiv in (b.get("per_model") or {}).items():
            rate = costs.input_rate(model, b["start"])
            if rate is None:
                continue
            total += equiv * rate / costs.PER_MILLION
            priced = True
    return (total / 24.0) if priced else None


def projection(conn, blocks=None, caps=None, live_tiles=None, now=None) -> dict:
    """The hero's figures, all off one basis (Decision 5).

    `burnt_equiv` / `left_equiv` / `runs_out_at` / `pct_of_block_per_hour` are
    each derived from exactly one of the two pinned quantities — the estimated
    cap and `burn_equiv_per_hour`. Re-deriving any of them from a separately
    summed ingest total would look equivalent and quietly disagree.
    """
    now = now or _now()
    if caps is None:
        caps = cap_estimates(conn, now=now)
    if blocks is None:
        blocks = _bucket(conn, None, TURN_DAYS, now)[0]

    wk = _pick(
        collapse_windows(store.latest_plan_windows(conn, now_iso=iso(now))), *WEEKLY_KEYS
    )
    used_pct = wk.get("used_pct")
    resets_at = wk.get("resets_at")
    reset_dt = parse_iso(resets_at)

    # H4 (`_headroom`'s own rule): a reading whose own window has already
    # reset describes a window that no longer exists, so it is never shown
    # as the current percentage -- same as headroom's stale-reset row goes
    # to unknown rather than keep printing the pre-reset figure.
    if reset_dt is not None and reset_dt < now:
        used_pct = None

    # `resets_at` is optional in the store: plan.extract keeps a row as soon as
    # used_pct parses, even when the reset epoch did not. No reset instant means
    # no clock to be ahead of or behind, and the verdict says so rather than
    # printing an arithmetic sentence with a hole in it.
    clock_pct = None
    if reset_dt is not None:
        span = WINDOW_HOURS["seven_day"] * 3600.0
        elapsed = span - (reset_dt - now).total_seconds()
        clock_pct = max(0.0, min(100.0, elapsed / span * 100.0))

    # A reading only moves when the /usage panel is opened again, so a
    # percentage that looks current can be days old while the week kept
    # burning underneath it. The age travels with the figure so the hero can
    # say how far it is allowed to be trusted instead of reading it as now.
    read_dt = parse_iso(wk.get("ts"))
    reading_age_hours = (
        None if read_dt is None else max(0.0, (now - read_dt).total_seconds() / 3600.0)
    )

    behind = None
    if used_pct is not None and clock_pct is not None:
        behind = used_pct - clock_pct  # negative = ahead of the clock, the good case

    cap7 = _pick(caps, *WEEKLY_KEYS).get("cap_equiv")
    cap5 = _pick(caps, *SESSION_KEYS).get("cap_equiv")
    burnt_equiv = left_equiv = None
    if cap7 is not None and used_pct is not None:
        burnt_equiv = cap7 * used_pct / 100.0
        left_equiv = cap7 - burnt_equiv

    burn = burn_equiv_per_hour(blocks, now=now, live_tiles=live_tiles)

    runs_out_dt = None
    if caps and burn > 0 and left_equiv is not None:
        runs_out_dt = now + _dt.timedelta(hours=max(0.0, left_equiv) / burn)

    # PLAN-dash-v2 6.4g #2: `runs_out_dt` after `resets_at` means the week
    # HOLDS at the current burn rate -- there is no "late" figure to report,
    # because the week resets before the projected exhaustion ever arrives.
    # early_hours is only ever a positive count of hours ahead of the reset.
    early_hours = None
    if runs_out_dt is not None and reset_dt is not None and runs_out_dt <= reset_dt:
        early_hours = (reset_dt - runs_out_dt).total_seconds() / 3600.0

    return {
        "used_pct": used_pct,
        "clock_pct": clock_pct,
        "reading_age_hours": reading_age_hours,
        "behind": behind,
        "burnt_equiv": burnt_equiv,
        "left_equiv": left_equiv,
        "runs_out_at": iso(runs_out_dt),
        "early_hours": early_hours,
        "resets_at": resets_at,
        "burn_equiv_per_hour": burn,
        "burn_usd_per_hour": _burn_usd_per_hour(blocks, live_tiles, now=now),
        "pct_of_block_per_hour": (burn / cap5 * 100.0) if cap5 else None,
    }


# --- 1.4 payload ------------------------------------------------------------


def _score(cell, cap5, provisional=False):
    """Attach `pct_of_cap` and `dry` to a merged cell.

    PLAN-dash-v2 6.4h H1: a cell's own denominator must scale with how many
    real 5-hour blocks it merged (§1.1's merge-not-drop rule), or a 3-block
    slot silently gets judged against a 1-block cap and reads 300%+. Never a
    display clamp -- the denominator itself is `blocks x cap5`. A cell with
    `blocks == 0` has no percentage at all, not a divide-by-zero guess.

    Dryness is still judged per real 5-hour BLOCK, never per merged cell: a
    slot holding two blocks can sum past the (correctly-scaled) cap without
    either window having got anywhere near it on its own, and calling that
    "ran dry" is a false claim, not a rounding error -- so `max_block_equiv`
    stays compared against the single-block `cap5`, unscaled, on purpose.
    With no cap both are dead — `dry` is False everywhere, because dryness is
    cap-relative by definition (§4). A `provisional` cap (a single
    low-percentage reading, PLAN-dash-v2 6.4g #1) still fills the bar, but
    must never itself assert "ran dry" -- one rounding step at a low
    percentage moves the cap by roughly a seventh of its own value.
    """
    if cell is None:
        return
    blocks = cell.get("blocks") or 0
    if not cap5 or not blocks:
        cell["pct_of_cap"] = None
        cell["dry"] = False
        return
    cell["pct_of_cap"] = cell["equiv_tokens"] / (cap5 * blocks) * 100.0
    cell["dry"] = False if provisional else (
        cell.get("max_block_equiv", 0.0) / cap5 * 100.0
    ) >= DRY_PCT


def _score_week(cell, cap7):
    """Attach `pct_of_week` to a merged cell (PLAN-fill-and-clock).

    Every window/half-day/slot the week grid and 30-day strip show is, by
    construction, a small slice of a week -- so scoring these panels against
    the 5-hour cap (`_score`'s `pct_of_cap`) left them all in the lowest band
    on a store whose real binding constraint is the WEEK. This is the same
    `equiv_tokens`, scored against the WEEKLY cap instead, purely for those
    two panels' fill.

    Unlike `pct_of_cap`, this is never scaled by `blocks`: `cap7` is one
    week-long allowance that does not multiply just because a slot happened
    to merge more than one real 5-hour block. `dry` is untouched here on
    purpose -- dryness stays `_score`'s claim about a single 5-hour block
    hitting its OWN cap, and this rescale must never start asserting a
    window ran dry when it did not.
    """
    if cell is None:
        return
    blocks = cell.get("blocks") or 0
    if not cap7 or not blocks:
        cell["pct_of_week"] = None
        return
    cell["pct_of_week"] = cell["equiv_tokens"] / cap7 * 100.0


def _dry_blocks(blocks, cap5, since_date):
    # A provisional cap5 (single-sample confidence, or revived from an earlier
    # run) is still a real number -- count against it, same as a confirmed
    # cap. The page labels the count PROVISIONAL via `cap5_provisional`
    # instead of this function withholding it as null (PLAN-dash round2 1).
    return sum(
        1 for b in blocks
        if b["local_date"] >= since_date and b["equiv_tokens"] / cap5 * 100.0 >= DRY_PCT
    )


def _headroom_short(key: str, cached: dict | None) -> str:
    """A short label distinct per window kind (PLAN-dash-v2 6.4h H5).

    plan.window_labels()'s "short" is the statusline's abbreviated tag
    ('5h'/'wk') -- both weekly windows collapse to the same 'wk' there,
    which is fine for a one-line statusline and useless for telling two
    headroom rows apart. This is a separate, headroom-only vocabulary.
    """
    if key in ("session", "five_hour"):
        return "5-hour block"
    if key in ("weekly_all", "seven_day"):
        return "Week \u00b7 all models"
    if key.startswith(plan_mod.SCOPED_PREFIX):
        model = (cached or {}).get("model") or key[len(plan_mod.SCOPED_PREFIX):]
        return "Week \u00b7 " + model + " only"
    return key.replace("_", " ")


def _headroom(conn, caps, now, cache_windows=None):
    """One row per window IDENTITY the feed actually reports (PLAN-window-
    identity 4) -- exactly the real windows, never one row per alias -- in
    plan.WINDOW_ORDER order over the raw key that won each identity.

    Labelled from the CURRENT /usage-cache reading when the identity is in it
    -- that carries the real parsed label ('This week · Fable', not a
    de-underscored key), because a scoped window's label is an account fact
    (the model's display_name) that plan.window_labels() cannot know from the
    key alone (PLAN-dash-v2 6.4f). Falls back to plan.window_labels() only for
    an identity the current cache read does not cover -- e.g. a hook-only
    window, or a stored snapshot from a cache reading that has since rotated
    out.

    PLAN-dash-v2 6.4h:
    H3 -- the bar and the note share ONE basis, the reading (more
    authoritative than a separately-counted token total). A counted total
    that contradicts the reading by more than a small tolerance falsifies
    the cap for THIS row rather than being printed beside it.
    H4 -- a reading whose own window has already reset describes a window
    that no longer exists; it is shown as unknown, not as current headroom.
    H5 -- see `_headroom_short`.
    H6 -- the note keeps its provenance ("from N readings") and the reset
    time, the way the mock's own note has both.
    """
    windows = collapse_windows(store.latest_plan_windows(conn))
    cache_windows = cache_windows or {}
    rows = []
    # Ordered by the IDENTITY itself, not by whichever raw key happened to
    # win that identity's row -- the two account-wide identities are already
    # spelled exactly as plan.WINDOW_ORDER's canonical names, so this stays
    # stable across reloads even when the winning alias flips between them.
    for ident in sorted(windows, key=window_order):
        w = windows[ident]
        key = w["raw_key"]
        used = w.get("used_pct")
        cap = (caps.get(ident) or {}).get("cap_equiv")
        samples = (caps.get(ident) or {}).get("samples")
        hours, part = window_span(key)
        reset_dt = parse_iso(w.get("resets_at"))
        span = _dt.timedelta(hours=hours) if hours else _dt.timedelta(hours=5)

        stale_reset = reset_dt is not None and reset_dt < now
        if stale_reset:
            used = None
            cap = None  # the whole row goes to unknown, not just the percentage

        left_pct = None if used is None else max(0.0, 100.0 - used)
        burnt = None if (cap is None or used is None) else cap * used / 100.0
        left_equiv = None if (cap is None or left_pct is None) else cap * left_pct / 100.0

        seen = None
        if not stale_reset:
            end = reset_dt or now
            seen = _equiv_in(conn, end - span, min(end, now), part)
            # more than ~20% beyond what the reading itself implies: the
            # cap/reading pairing for this one row is not trustworthy.
            if cap is not None and burnt is not None and seen > burnt * 1.2 + 1.0:
                cap = burnt = left_equiv = None

        # Prefer the cache's OWN name for this identity (the /usage cache's
        # "limits" extraction keys by "session"/"weekly_all"/"weekly_scoped_*"
        # -- the same strings `identity_of` uses), so a cache label is not
        # lost just because a newer statusline-feed reading (a different raw
        # key, same identity) won the row. Falls back to the winning raw key
        # for the cache's own named-window fallback shape, where the cache
        # itself writes "five_hour"/"seven_day".
        cached = cache_windows.get(ident) or cache_windows.get(key)
        if cached and cached.get("label"):
            label = cached["label"]
        else:
            _unused_short, label = plan_mod.window_labels(key)
        short = _headroom_short(key, cached)

        # H4b: a reading inside a still-open window can still be hours old --
        # only the /usage panel moves it. Age is stated whenever it rounds to
        # an hour or more, so a figure is never read as "as of now" when it
        # is not.
        read_dt = parse_iso(w.get("ts"))
        age_hours = (
            None if read_dt is None else max(0.0, (now - read_dt).total_seconds() / 3600.0)
        )
        age_bit = ""
        if age_hours is not None and age_hours >= 1:
            age_bit = " · read %dh ago" % round(age_hours)

        if stale_reset:
            note = "this reading has already reset — waiting for a fresh /usage read"
        elif cap is None:
            note = ((("%s seen in this window" % costs.fmt_tokens(seen)) + age_bit)
                    if seen is not None else "cap unknown for this window")
        else:
            reset_bit = ""
            if reset_dt is not None:
                # A weekly window resets days out, not tonight -- a bare
                # "resets 17:30" reads as "later today" even when it is not.
                # Say the weekday too whenever the reset lands on a
                # different local day than `now`; today's own reset stays
                # bare since the day is already implied.
                local_reset = _local(reset_dt)
                if local_reset.date() != _local(now).date():
                    reset_bit = " · resets " + local_reset.strftime("%a %H:%M")
                else:
                    reset_bit = " · resets " + local_reset.strftime("%H:%M")
            reading_bit = ""
            if samples:
                reading_bit = " \u00b7 from %d reading%s" % (samples, "" if samples == 1 else "s")
            note = "%s of ≈%s%s%s%s" % (
                costs.fmt_tokens(burnt), costs.fmt_tokens(cap), reset_bit, reading_bit,
                age_bit,
            )

        rows.append({
            "key": key,
            "short_label": short,
            "label": label,
            "used_pct": used,
            "left_pct": left_pct,
            "left_equiv": left_equiv,
            "cap_equiv": cap,
            "resets_at": w.get("resets_at"),
            "window_equiv": burnt,
            "age_hours": age_hours,
            "note": note,
        })
    return rows

def _source_kind(conn) -> str | None:
    """"hook" | None. Used to read "cache" | "hook" | "both", resolved by
    availability of a second, optional local source: a snapshot Claude Code
    wrote to ~/.claude.json's `cachedUsageUtilization` block. That source was
    dropped (session 2026-08-09) -- it is a snapshot that only moves when
    someone opens /usage, and a stale local copy was being read as if live.
    Windows now come only from the status-line feed, so this collapses to
    whether the store holds ANY window key at all.
    """
    return "hook" if store.latest_plan_windows(conn) else None


def _hook_fetched_at(conn) -> str | None:
    """Newest `ts` across every stored window, the status-line feed's own
    answer to "how fresh is this" now that the /usage-cache's `fetched_at` is
    no longer a source (see `_source_kind`)."""
    stored = store.latest_plan_windows(conn)
    timestamps = [w["ts"] for w in stored.values() if w.get("ts")]
    return max(timestamps) if timestamps else None


def windows_payload(conn, project=None, now=None, live_tiles=None) -> dict:
    """Everything the mock's quota panels need, in one response."""
    now = now or _now()
    blocks, anchor, anchor_at = _bucket(conn, project, TURN_DAYS, now)
    caps_before_falsify = cap_estimates(conn, now=now)
    caps = falsify_stale_caps(caps_before_falsify, blocks, now)
    caps_known = bool(caps)
    cap5_info = _pick(caps, *SESSION_KEYS)
    # A stale/reset current reading (or a falsified estimate) can drop this
    # run's cap5 while an earlier run already derived a real one -- reuse it
    # for dry counts and grid fills rather than losing every one of them to a
    # merely-stale reading (PLAN-dash-ui-fixes 5). Provisional through the
    # existing flag; `caps_before_falsify` is still real, derived evidence,
    # never a fabricated cap -- just possibly for a window that has since
    # reset or been superseded.
    cap5_revived = False
    if not cap5_info:
        cap5_info = _pick(caps_before_falsify, *SESSION_KEYS)
        cap5_provisional = bool(cap5_info)
        cap5_revived = bool(cap5_info)
    else:
        cap5_provisional = cap5_info.get("confidence") == "single"
    cap5 = cap5_info.get("cap_equiv")
    # `_headroom` reads its own per-row cap out of `caps` (never `caps` itself
    # here -- that dict also drives `caps_known`/`cap7` and must stay exactly
    # what falsification left it). A shallow copy carries the revived cap5 to
    # the 5-HOUR BLOCK row too, so it can show a provisional percentage
    # instead of "cap unknown" right beside dry counts that now say the same.
    caps_for_headroom = caps
    if cap5_revived:
        caps_for_headroom = dict(caps)
        caps_for_headroom[SESSION_KEYS[0]] = cap5_info
    # PLAN-fill-and-clock: the WEEKLY cap, used only to rescale the week grid
    # and 30-day strip fills (see `_score_week`) -- never touches `_score`'s
    # 5-hour-cap `pct_of_cap`/`dry`.
    cap7 = _pick(caps, *WEEKLY_KEYS).get("cap_equiv") if caps_known else None

    hero = projection(conn, blocks=blocks, caps=caps, live_tiles=live_tiles, now=now)

    week = week_grid(blocks, now=now, resets_at=hero.get("resets_at"))
    for row in week:
        for cell in row["cells"]:
            _score(cell, cap5, provisional=cap5_provisional)
            _score_week(cell, cap7)

    horizon = horizon_cells(blocks, now=now)
    for cell in horizon:
        _score(cell, cap5, provisional=cap5_provisional)
        _score_week(cell, cap7)

    today = _local(now).date()
    # week_from tracks the SAME 7 dates week_grid just drew (Bug 3: the grid
    # anchors to resets_at, not to today) -- otherwise week_blocks/dry_count
    # would count a different span than the row labels on screen.
    week_from = week[0]["date"]
    month_from = (today - _dt.timedelta(days=HORIZON_DAYS - 1)).isoformat()
    week_blocks = [b for b in blocks if b["local_date"] >= week_from]

    # Top 8 of the week's cells, ranked by the very number the table prints.
    # The USED column shows `pct_of_cap`, and `_score` divides by
    # `cap5 * blocks`, so a merged multi-block cell can hold the most raw
    # tokens while showing the smallest percentage -- ranking by
    # `equiv_tokens` put a column reading 9, 18, 15, 13, 9, 6, 6, 2 under a
    # heading that says "heaviest". The sort key has to be the visible
    # quantity or the heading is a false claim. `equiv_tokens` stays on as the
    # next key because with no cap every `pct_of_cap` is None and the column
    # falls back to tokens -- the ranking then follows the column there too.
    # Cells with no percentage sort last instead of raising on None vs float;
    # ties are still broken by recency.
    flat = [c for row in week for c in row["cells"] if c is not None]
    flat.sort(
        key=lambda c: (
            c.get("pct_of_cap") is not None,
            c.get("pct_of_cap") if c.get("pct_of_cap") is not None else 0.0,
            c["equiv_tokens"],
            c["start"],
        ),
        reverse=True,
    )
    heaviest = []
    for c in flat[:HEAVIEST]:
        heaviest.append({
            "name": f"{c['day']} {c['time']}",
            "day": c["day"], "time": c["time"], "date": c["local_date"],
            "equiv_tokens": c["equiv_tokens"], "turns": c["turns"],
            "leader": c["leader"], "blocks": c["blocks"],
            "pct_of_cap": c["pct_of_cap"], "dry": c["dry"],
            "pct_of_week": c.get("pct_of_week"),
        })

    # The true leader of each cell. The mock force-attributes dry cells to one
    # model (`li: dry ? 1 : leaderOf(...)`); that is a visual shortcut in a
    # sample, and porting it would put a model's name on windows it did not
    # lead. Dryness is carried by the cell's edge instead.
    led: dict[str, int] = {}
    for c in flat:
        if c["leader"]:
            led[c["leader"]] = led.get(c["leader"], 0) + 1

    current = None
    if blocks:
        n = int((now - anchor_at).total_seconds() // (BLOCK_HOURS * 3600.0))
        start = anchor_at + _dt.timedelta(seconds=BLOCK_HOURS * 3600.0 * n)
        here = next((b for b in blocks if b["start"] == iso(start)), None)
        current = {
            "start": iso(start),
            "ends_at": iso(start + BLOCK),
            "equiv_tokens": here["equiv_tokens"] if here else 0.0,
            "turns": here["turns"] if here else 0,
            "pct_of_cap": None,
            "lands_at_pct": None,
        }
        if cap5:
            current["pct_of_cap"] = current["equiv_tokens"] / cap5 * 100.0
            hours_left = max(0.0, ((start + BLOCK) - now).total_seconds() / 3600.0)
            current["lands_at_pct"] = (
                (current["equiv_tokens"] + hero["burn_equiv_per_hour"] * hours_left)
                / cap5 * 100.0
            )

    return {
        "scope": project,
        "generated": iso(now),
        # `caps_known` is bool(caps) and is stated once, here. Nothing else
        # restates the same condition in different words.
        "caps_known": caps_known,
        "caps": caps,
        # A hook that has run but never crossed 5% on any window still yields
        # caps == {} — "collecting", which is a different sentence from
        # "never set up", so the page can tell them apart.
        "hook_ran": bool(store.latest_plan_windows(conn)),
        "setup_cmd": SETUP_CMD,
        "anchor": anchor,
        "hero": hero,
        "headroom": _headroom(conn, caps_for_headroom, now),
        "week": week,
        "week_blocks": len(week_blocks),
        "current_block": current,
        "heaviest": heaviest,
        "horizon": horizon,
        # null, not 0: 0 claims "checked, found none"; null says "unknown",
        # which is the only honest answer without a cap.
        "dry_count_week": _dry_blocks(week_blocks, cap5, week_from) if cap5 else None,
        "dry_count_month": _dry_blocks(blocks, cap5, month_from) if cap5 else None,
        # A provisional cap5 (single-sample confidence, or revived from an
        # earlier run) no longer suppresses the dry counts above -- they are
        # real numbers either way. Carried here so the page can label them
        # PROVISIONAL instead of asserting a confidence the cap doesn't have
        # yet (PLAN-dash round2 1, superseding PLAN-dash-ui-fixes 5 rule 3).
        "cap5_provisional": bool(cap5) and cap5_provisional,
        "led_counts": led,
        "block_hours": BLOCK_HOURS,
        "horizon_days": HORIZON_DAYS,
        "slot_labels": list(SLOT_LABELS),
        # PLAN-dash-v2 5: tier/credits/source/fetched_at, so the page can say
        # how fresh the quota numbers are. None when the source is absent --
        # never a guess. `source`/`fetched_at` used to also read the /usage
        # cache (~/.claude.json's `cachedUsageUtilization`), but that file is
        # a snapshot that only moves when someone opens /usage -- a stale
        # local copy read as if live (session 2026-08-09: four days old
        # against a ten-minute-old live number). Windows now come from the
        # status-line feed alone (`plan_snapshots`, written by the hook on
        # every statusline redraw); `tier`/`credits` still read the cache
        # directly since those aren't window percentages and have no
        # statusline equivalent to fall back to.
        "tier": plan_mod.plan_tier(),
        "credits": plan_mod.usage_credits(),
        "source": _source_kind(conn),
        "fetched_at": _hook_fetched_at(conn),
    }
