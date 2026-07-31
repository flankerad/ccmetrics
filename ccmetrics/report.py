"""Console renderer (R6 surface 1): `ccmetrics` inside a repo.

Cost confidence is always visible. The floor is always written as "at least $X";
the output estimate is always a separate range; anything not derivable prints
"unknown" and is never filled in with a guess.
"""

from __future__ import annotations

import os
import sqlite3

import json

from . import constants, costs, detectors, ingest, store

WINDOW_DAYS = constants.value(constants.RETENTION["turn_days"])


def current_project_key() -> str:
    return ingest.encode_project(os.getcwd())


def known_projects(conn: sqlite3.Connection) -> set[str]:
    return {r["project"] for r in conn.execute("SELECT DISTINCT project FROM daily")}


def _rows(conn: sqlite3.Connection, project: str | None):
    """Per (model, day): a model whose price changed must be priced by the day
    the turns actually ran, so pricing never groups across a rate boundary."""
    sql = (
        "SELECT model, substr(ts,1,10) day, COUNT(*) turns, SUM(cw5m) cw5m, "
        "SUM(cw1h) cw1h, SUM(cread) cread, SUM(out_bytes) out_bytes, "
        "SUM(raw_in) raw_in, SUM(raw_out) raw_out, SUM(sidechain) sidechain FROM turns"
    )
    args: tuple = ()
    if project:
        sql += " WHERE project = ?"
        args = (project,)
    sql += " GROUP BY model, day ORDER BY cread DESC"
    return conn.execute(sql, args).fetchall()


EXACT_COVERAGE_MIN = constants.value(constants.OTEL["exact_coverage_min"])


def exact_window(
    conn: sqlite3.Connection, project: str | None, days: int = WINDOW_DAYS
) -> dict:
    """OTEL coverage for the reporting window (wave D).

    A day is exact-covered when Anthropic's own api_request events span at least
    95% of that day's turns. Covered days report Anthropic's dollars; every other
    day stays a floor. The two are never added together behind the user's back —
    a mixed window reports both numbers and says which days each covers.

    mode: "none" (no telemetry at all — v0.1.0 behaviour, unchanged),
          "exact" (every day with turns is covered), "mixed" (some of each).
    """
    import datetime as _dt

    start = (_dt.date.today() - _dt.timedelta(days=days - 1)).isoformat()
    sql = (
        "SELECT date, SUM(turns) turns, SUM(exact_events) events, "
        "SUM(exact_usd) exact_usd, SUM(floor_usd) floor_usd, "
        "SUM(CASE WHEN floor_usd IS NULL THEN 1 ELSE 0 END) unpriced "
        "FROM daily WHERE date >= ?"
    )
    args: list = [start]
    if project:
        sql += " AND project = ?"
        args.append(project)
    sql += " GROUP BY date ORDER BY date"

    out = {
        "available": False,
        "mode": "none",
        "coverage_min": EXACT_COVERAGE_MIN,
        "events": 0,
        "exact_usd": 0.0,          # covered days only
        "floor_usd_covered": 0.0,  # the floor those same days would have shown
        "floor_usd_uncovered": 0.0,
        "floor_uncovered_priced": True,
        "days_covered": 0,
        "days_uncovered": 0,
        "days_partial": 0,
        "covered_dates": [],
    }
    for r in conn.execute(sql, args):
        turns = r["turns"] or 0
        events = r["events"] or 0
        floor = None if r["unpriced"] else (r["floor_usd"] or 0.0)
        out["events"] += events
        if events and (turns == 0 or events / turns >= EXACT_COVERAGE_MIN):
            out["days_covered"] += 1
            out["covered_dates"].append(r["date"])
            out["exact_usd"] += r["exact_usd"] or 0.0
            if floor is not None:
                out["floor_usd_covered"] += floor
            continue
        if events:
            out["days_partial"] += 1
        if turns <= 0:
            continue
        out["days_uncovered"] += 1
        if floor is None:
            out["floor_uncovered_priced"] = False
        else:
            out["floor_usd_uncovered"] += floor
    if out["events"] and out["days_covered"]:
        out["available"] = True
        out["mode"] = "exact" if out["days_uncovered"] == 0 else "mixed"
    elif out["events"]:
        # telemetry arrived but no day cleared the bar: still a floor report,
        # with the partial coverage named rather than quietly folded in.
        out["available"] = True
        out["mode"] = "mixed"
    return out


def summary(conn: sqlite3.Connection, project: str | None) -> dict:
    by_model: dict[str, dict] = {}
    tot = {"turns": 0, "cw5m": 0, "cw1h": 0, "cread": 0, "out_bytes": 0, "sidechain": 0}
    floor = 0.0
    floor_unknown_tokens = 0
    est_lo = est_hi = 0.0
    est_unknown_bytes = 0
    for r in _rows(conn, project):
        model = r["model"]
        day = r["day"]
        cw5m, cw1h, cread = r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0
        ob = r["out_bytes"] or 0
        f = costs.floor_usd(model, cw5m, cw1h, cread, day)
        e = costs.output_estimate_usd(model, ob, day)
        if f is None:
            floor_unknown_tokens += int(costs.billable_input_equivalent(cw5m, cw1h, cread))
        else:
            floor += f
        if e is None:
            est_unknown_bytes += ob
        else:
            est_lo += e[0]
            est_hi += e[1]
        m = by_model.setdefault(
            model,
            {"model": model, "turns": 0, "cw5m": 0, "cw1h": 0, "cread": 0,
             "out_bytes": 0, "floor_usd": 0.0, "priced": True},
        )
        m["turns"] += r["turns"]
        m["cw5m"] += cw5m
        m["cw1h"] += cw1h
        m["cread"] += cread
        m["out_bytes"] += ob
        if f is None:
            m["priced"] = False
            m["floor_usd"] = None
        elif m["priced"]:
            m["floor_usd"] += f
        tot["turns"] += r["turns"]
        for k in ("cw5m", "cw1h", "cread", "out_bytes", "sidechain"):
            tot[k] += r[k] or 0
    per_model = list(by_model.values())

    where = " WHERE project = ?" if project else ""
    args = (project,) if project else ()
    srow = conn.execute(
        "SELECT COUNT(*) n, SUM(compactions) c, SUM(precompact_tokens) p FROM sessions" + where,
        args,
    ).fetchone()

    return {
        "project": project,
        "window_days": WINDOW_DAYS,
        "totals": tot,
        "per_model": per_model,
        "floor_usd": floor,
        "floor_priced": floor_unknown_tokens == 0,
        "floor_unknown_equiv_tokens": floor_unknown_tokens,
        "est_output_usd": (est_lo, est_hi),
        "est_unknown_bytes": est_unknown_bytes,
        "billable_equiv": costs.billable_input_equivalent(
            tot["cw5m"], tot["cw1h"], tot["cread"]
        ),
        "cache_hit": costs.cache_hit_ratio(tot["cread"], tot["cw5m"] + tot["cw1h"]),
        "sessions": srow["n"] or 0,
        "compactions": srow["c"] or 0,
        "precompact_tokens": srow["p"] or 0,
        "exact": exact_window(conn, project),
    }


def top_projects(conn: sqlite3.Connection, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT project, SUM(cw5m) cw5m, SUM(cw1h) cw1h, SUM(cread) cread, "
        "COUNT(*) turns FROM turns GROUP BY project"
    ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "project": r["project"],
                "turns": r["turns"],
                "equiv": costs.billable_input_equivalent(
                    r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0
                ),
                "cread": r["cread"] or 0,
            }
        )
    out.sort(key=lambda d: d["equiv"], reverse=True)
    return out[:limit]


# --- findings (PRD R3/R4b/R6) ----------------------------------------------


def leaks(conn: sqlite3.Connection, project: str | None, limit: int | None = 3) -> list[dict]:
    """Top findings for this scope, ranked by tokens saved / effort tier.

    Scope rule (R6): inside a known repo the console shows that repo's findings;
    otherwise every project's. Line items (detector 10) never headline.
    """
    found = store.load_findings(conn, project)
    ranked = detectors.rank(found)
    return ranked if limit is None else ranked[:limit]


def all_leaks(conn: sqlite3.Connection, project: str | None) -> list[dict]:
    found = store.load_findings(conn, project)
    found = [f for f in found if f["tokens_saved"]]
    found.sort(key=detectors.score, reverse=True)
    return found


def _evidence(f: dict) -> dict:
    try:
        return json.loads(f["evidence"] or "{}")
    except (TypeError, ValueError):
        return {}


def render_leaks(found: list[dict], scoped: bool, indent: str = "  ") -> list[str]:
    lines: list[str] = []
    if not found:
        lines.append(f"{indent}nothing worth flagging in this window.")
        return lines
    for i, f in enumerate(found, 1):
        ev = _evidence(f)
        name = ev.get("detector_name", f"detector {f['detector']}")
        effort = {1: "paste", 3: "habit", 10: "restructure"}.get(f["effort"], f["effort"])
        usd = costs.fmt_usd(f["usd_saved"]) if f["usd_saved"] is not None else "unknown"
        head = (
            f"{indent}{i}. {name:<34} ~{costs.fmt_tokens(f['tokens_saved']):>7} tok  "
            f"{usd:>8}  effort:{effort}"
        )
        if not scoped:
            head += f"  [{f['project']}]"
        lines.append(head)
        for row in (f["fix_text"] or "").splitlines():
            lines.append(f"{indent}   {row}")
        lines.append("")
    return lines


# --- rendering --------------------------------------------------------------

BAR_CHARS = "█▓░"


def _mix_bar(read: int, w5: int, w1: int, width: int = 24) -> str:
    total = read + w5 + w1
    if total <= 0:
        return "░" * width
    parts = [round(width * read / total), round(width * w5 / total)]
    parts.append(max(0, width - parts[0] - parts[1]))
    return "█" * parts[0] + "▓" * parts[1] + "▒" * parts[2]


def confidence_label(exact: dict | None) -> str:
    """One phrase for the whole window's cost confidence (console chip + dash)."""
    if not exact or not exact.get("available"):
        return "at least this much · from session files"
    if exact["mode"] == "exact":
        return "exact (OTEL)"
    return (
        f"mixed · exact (OTEL) on {exact['days_covered']}d, "
        f"at-least figures elsewhere"
    )


def _exact_lines(exact: dict | None) -> list[str]:
    """The OTEL block under SPEND. Absent telemetry, this is the same single
    confidence line v0.1.0 printed — nothing about the floor report changes."""
    if not exact or not exact.get("available"):
        return [
            "        how sure: what this usage is worth at API prices, a proven "
            "minimum from your session files — on a subscription your bill is the "
            "flat fee (run `ccmetrics otel --setup` for the exact figures)"
        ]
    covered = exact["days_covered"]
    pct = int(round(exact["coverage_min"] * 100))
    lines = []
    if exact["mode"] == "exact":
        lines.append(
            f"        {costs.fmt_usd(exact['exact_usd'])} exact (OTEL) — "
            f"Anthropic's own per-request cost, all {covered} covered day(s)"
        )
        lines.append(
            f"        how sure: exact (OTEL) · without it we could only say you "
            f"paid at least {costs.fmt_usd(exact['floor_usd_covered'])} on the "
            f"same days"
        )
        return lines
    if covered:
        lines.append(
            f"        {costs.fmt_usd(exact['exact_usd'])} exact (OTEL) for "
            f"{covered} day(s) · at least "
            f"{costs.fmt_usd(exact['floor_usd_uncovered']) if exact['floor_uncovered_priced'] else 'unknown'}"
            f" for the other {exact['days_uncovered']}"
        )
        lines.append(
            "        how sure: mixed — the two numbers above cover different days, "
            "so they are never added together"
        )
    else:
        lines.append(
            f"        {exact['events']:,} OTEL events received, but no day is "
            f"{pct}% covered — so the report still says 'at least'"
        )
        lines.append(
            "        how sure: what this usage is worth at API prices, a proven "
            "minimum (some telemetry arrived, not enough to price a whole day)"
        )
    return lines


def render(
    s: dict,
    projects: list[dict] | None = None,
    db_size: int | None = None,
    found: list[dict] | None = None,
) -> str:
    t = s["totals"]
    scope = s["project"] or "all projects"
    lines = []
    lines.append(f"ccmetrics · {scope} · last {s['window_days']} days")
    lines.append("")

    priced_turns = sum(m["turns"] for m in s["per_model"] if m["priced"])
    coverage = costs.fmt_pct(priced_turns, t["turns"])
    floor_txt = "at least " + costs.fmt_usd(s["floor_usd"])
    if not s["floor_priced"]:
        floor_txt += f"  ({coverage} of turns priced — see MODELS)"
    est = s["est_output_usd"]
    est_txt = costs.fmt_usd_range(est) if (est[0] or est[1]) else "unknown"
    lines.append(f"SPEND   {floor_txt}")
    if not s["floor_priced"]:
        lines.append(
            f"        unknown  {costs.fmt_tokens(s['floor_unknown_equiv_tokens'])} "
            f"tokens on models with no price listed in constants.py "
            f"(never guessed)"
        )
    lines.append(
        f"        + est. output {est_txt}   (a range, never added to the number above)"
    )
    lines.extend(_exact_lines(s.get("exact")))
    lines.append("")

    lines.append(
        f"TOKENS  {_mix_bar(t['cread'], t['cw5m'], t['cw1h'])}  "
        f"read {costs.fmt_tokens(t['cread'])} █ · write-5m {costs.fmt_tokens(t['cw5m'])} ▓ · "
        f"write-1h {costs.fmt_tokens(t['cw1h'])} ▒"
    )
    hit = s["cache_hit"]
    lines.append(
        f"        {costs.fmt_tokens(s['billable_equiv'])} tokens' worth of cost · "
        f"cache-hit {'unknown' if hit is None else f'{hit*100:.0f}%'} · "
        f"est. output {costs.fmt_tokens(costs.output_token_range(t['out_bytes'])[0])}–"
        f"{costs.fmt_tokens(costs.output_token_range(t['out_bytes'])[1])} tok"
    )
    lines.append(
        f"        {t['turns']:,} turns · {s['sessions']:,} sessions · "
        f"{t['sidechain']:,} sidechain turns · {s['compactions']:,} compactions "
        f"({costs.fmt_tokens(s['precompact_tokens'])} pre-compact tokens)"
    )
    lines.append("")

    if s["per_model"]:
        lines.append("MODELS  turns    cache-read   write-5m    write-1h   at least $")
        for m in sorted(s["per_model"], key=lambda d: d["cread"], reverse=True):
            lines.append(
                f"        {m['turns']:>6}  {costs.fmt_tokens(m['cread']):>10}  "
                f"{costs.fmt_tokens(m['cw5m']):>9}  {costs.fmt_tokens(m['cw1h']):>9}  "
                f"{costs.fmt_usd(m['floor_usd']):>9}  {m['model']}"
            )
        lines.append("")

    if projects:
        lines.append("TOP PROJECTS (by tokens' worth of input cost)")
        for i, p in enumerate(projects, 1):
            lines.append(
                f"  {i}. {costs.fmt_tokens(p['equiv']):>8}  {p['turns']:>6} turns  {p['project']}"
            )
        lines.append("")

    lines.append("TOP LEAKS (ranked by tokens saved vs how hard the fix is)")
    lines.extend(render_leaks(found or [], scoped=s["project"] is not None))
    lines.append("run `ccmetrics --all-leaks` for every finding, `ccmetrics constants` for sources")
    if db_size is not None:
        lines.append(f"state db {db_size/1e6:.1f} MB")
    return "\n".join(lines)
