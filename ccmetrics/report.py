"""Console renderer (R6 surface 1): `ccmetrics` inside a repo.

Cost confidence is always visible. The floor is always labelled "floor"; the
output estimate is always a separate range; anything not derivable prints
"unknown" and is never filled in with a guess.
"""

from __future__ import annotations

import os
import sqlite3

from . import constants, costs, ingest

WINDOW_DAYS = constants.value(constants.RETENTION["turn_days"])


def current_project_key() -> str:
    return ingest.encode_project(os.getcwd())


def known_projects(conn: sqlite3.Connection) -> set[str]:
    return {r["project"] for r in conn.execute("SELECT DISTINCT project FROM daily")}


def _rows(conn: sqlite3.Connection, project: str | None):
    sql = (
        "SELECT model, COUNT(*) turns, SUM(cw5m) cw5m, SUM(cw1h) cw1h, SUM(cread) cread, "
        "SUM(out_bytes) out_bytes, SUM(raw_in) raw_in, SUM(raw_out) raw_out, "
        "SUM(sidechain) sidechain FROM turns"
    )
    args: tuple = ()
    if project:
        sql += " WHERE project = ?"
        args = (project,)
    sql += " GROUP BY model ORDER BY cread DESC"
    return conn.execute(sql, args).fetchall()


def summary(conn: sqlite3.Connection, project: str | None) -> dict:
    per_model = []
    tot = {"turns": 0, "cw5m": 0, "cw1h": 0, "cread": 0, "out_bytes": 0, "sidechain": 0}
    floor = 0.0
    floor_unknown_tokens = 0
    est_lo = est_hi = 0.0
    est_unknown_bytes = 0
    for r in _rows(conn, project):
        model = r["model"]
        cw5m, cw1h, cread = r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0
        ob = r["out_bytes"] or 0
        f = costs.floor_usd(model, cw5m, cw1h, cread)
        e = costs.output_estimate_usd(model, ob)
        if f is None:
            floor_unknown_tokens += int(costs.billable_input_equivalent(cw5m, cw1h, cread))
        else:
            floor += f
        if e is None:
            est_unknown_bytes += ob
        else:
            est_lo += e[0]
            est_hi += e[1]
        per_model.append(
            {
                "model": model,
                "turns": r["turns"],
                "cw5m": cw5m,
                "cw1h": cw1h,
                "cread": cread,
                "out_bytes": ob,
                "floor_usd": f,
                "priced": f is not None,
            }
        )
        tot["turns"] += r["turns"]
        for k in ("cw5m", "cw1h", "cread", "out_bytes", "sidechain"):
            tot[k] += r[k] or 0

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


# --- rendering --------------------------------------------------------------

BAR_CHARS = "█▓░"


def _mix_bar(read: int, w5: int, w1: int, width: int = 24) -> str:
    total = read + w5 + w1
    if total <= 0:
        return "░" * width
    parts = [round(width * read / total), round(width * w5 / total)]
    parts.append(max(0, width - parts[0] - parts[1]))
    return "█" * parts[0] + "▓" * parts[1] + "▒" * parts[2]


def render(s: dict, projects: list[dict] | None = None, db_size: int | None = None) -> str:
    t = s["totals"]
    scope = s["project"] or "all projects"
    lines = []
    lines.append(f"ccmetrics · {scope} · last {s['window_days']} days")
    lines.append("")

    priced_turns = sum(m["turns"] for m in s["per_model"] if m["priced"])
    coverage = costs.fmt_pct(priced_turns, t["turns"])
    floor_txt = costs.fmt_usd(s["floor_usd"]) + " floor"
    if not s["floor_priced"]:
        floor_txt += f"  ({coverage} of turns priced — see MODELS)"
    est = s["est_output_usd"]
    est_txt = costs.fmt_usd_range(est) if (est[0] or est[1]) else "unknown"
    lines.append(f"SPEND   {floor_txt}")
    if not s["floor_priced"]:
        lines.append(
            f"        unknown  {costs.fmt_tokens(s['floor_unknown_equiv_tokens'])} "
            f"billable-equiv input tokens on models with no rate in constants.py "
            f"(never guessed)"
        )
    lines.append(f"        + est. output {est_txt}   (range, never added to the floor)")
    lines.append("        cost confidence: approximate · JSONL-only, cache fields only (no OTEL)")
    lines.append("")

    lines.append(
        f"TOKENS  {_mix_bar(t['cread'], t['cw5m'], t['cw1h'])}  "
        f"read {costs.fmt_tokens(t['cread'])} █ · write-5m {costs.fmt_tokens(t['cw5m'])} ▓ · "
        f"write-1h {costs.fmt_tokens(t['cw1h'])} ▒"
    )
    hit = s["cache_hit"]
    lines.append(
        f"        billable-equiv {costs.fmt_tokens(s['billable_equiv'])} · "
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
        lines.append("MODELS  turns    cache-read   write-5m    write-1h    floor")
        for m in sorted(s["per_model"], key=lambda d: d["cread"], reverse=True):
            lines.append(
                f"        {m['turns']:>6}  {costs.fmt_tokens(m['cread']):>10}  "
                f"{costs.fmt_tokens(m['cw5m']):>9}  {costs.fmt_tokens(m['cw1h']):>9}  "
                f"{costs.fmt_usd(m['floor_usd']):>9}  {m['model']}"
            )
        lines.append("")

    if projects:
        lines.append("TOP PROJECTS (by billable-equivalent input tokens)")
        for i, p in enumerate(projects, 1):
            lines.append(
                f"  {i}. {costs.fmt_tokens(p['equiv']):>8}  {p['turns']:>6} turns  {p['project']}"
            )
        lines.append("")

    lines.append("TOP LEAKS  —  detectors land in wave B")
    if db_size is not None:
        lines.append(f"state db {db_size/1e6:.1f} MB")
    return "\n".join(lines)
