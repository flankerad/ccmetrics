"""Local dashboard: ThreadingHTTPServer bound to 127.0.0.1 only.

Endpoints (JSON, metadata only — counts, byte sizes, timestamps, model names,
project keys, file paths, detector names and our own fix templates; never a
byte of message text):

    GET /                      static/index.html (single self-contained file)
    GET /api/summary           global 30-day view
    GET /api/project/<key>     the same shape, scoped to one project
    GET /api/projects          per-project table rows + delta vs prior 30 days
    GET /api/findings[?project=]  ranked findings with fix text
    GET /api/live              R7 live tiles
    GET /api/constants         provenance table (every constant + source URL)

Binding is hard-coded to 127.0.0.1: there is no flag that exposes this on a
network interface. Nothing is written to ~/.claude; the store is opened
read-only in practice (queries only) and per thread.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__, constants, costs, detectors, live, report, store

HOST = "127.0.0.1"
DEFAULT_PORT = 7433
STATIC_DIR = Path(__file__).resolve().parent / "static"
WINDOW_DAYS = constants.value(constants.RETENTION["turn_days"])

_local = threading.local()


def _conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = store.connect()
        _local.conn = conn
    return conn


# --- payload builders -------------------------------------------------------


def _iso_days(n: int) -> list[str]:
    import datetime as _dt

    today = _dt.date.today()
    return [(today - _dt.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]


def daily_series(conn: sqlite3.Connection, project: str | None, days: int = WINDOW_DAYS) -> list[dict]:
    """Per-day floor $ + bounded output estimate + token mix, priced per model
    per day so a model that changed price never groups across the boundary."""
    axis = _iso_days(days)
    first = axis[0]
    sql = (
        "SELECT substr(ts,1,10) day, model, SUM(cw5m) cw5m, SUM(cw1h) cw1h, "
        "SUM(cread) cread, SUM(out_bytes) out_bytes, COUNT(*) turns "
        "FROM turns WHERE ts >= ?"
    )
    args: list = [first]
    if project:
        sql += " AND project = ?"
        args.append(project)
    sql += " GROUP BY day, model"
    acc = {
        d: {"date": d, "floor_usd": 0.0, "floor_priced": True, "est_lo": 0.0,
            "est_hi": 0.0, "cw5m": 0, "cw1h": 0, "cread": 0, "out_bytes": 0, "turns": 0,
            "exact_usd": None, "exact_events": 0, "exact_coverage": None,
            "exact_covered": False}
        for d in axis
    }
    for r in conn.execute(sql, args):
        row = acc.get(r["day"])
        if row is None:
            continue
        cw5m, cw1h, cread = r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0
        ob = r["out_bytes"] or 0
        f = costs.floor_usd(r["model"], cw5m, cw1h, cread, r["day"])
        if f is None:
            row["floor_priced"] = False
        else:
            row["floor_usd"] += f
        e = costs.output_estimate_usd(r["model"], ob, r["day"])
        if e:
            row["est_lo"] += e[0]
            row["est_hi"] += e[1]
        row["cw5m"] += cw5m
        row["cw1h"] += cw1h
        row["cread"] += cread
        row["out_bytes"] += ob
        row["turns"] += r["turns"]
    # Wave D: Anthropic's own dollars per day, where telemetry covered the day.
    esql = (
        "SELECT date, SUM(exact_usd) usd, SUM(exact_events) events, SUM(turns) turns "
        "FROM daily WHERE date >= ? AND exact_events > 0"
    )
    eargs: list = [first]
    if project:
        esql += " AND project = ?"
        eargs.append(project)
    esql += " GROUP BY date"
    for r in conn.execute(esql, eargs):
        row = acc.get(r["date"])
        if row is None:
            continue
        turns, events = r["turns"] or 0, r["events"] or 0
        cov = 1.0 if turns == 0 else events / turns
        row["exact_usd"] = r["usd"]
        row["exact_events"] = events
        row["exact_coverage"] = cov
        row["exact_covered"] = cov >= report.EXACT_COVERAGE_MIN

    out = []
    for d in axis:
        row = acc[d]
        row["equiv"] = costs.billable_input_equivalent(row["cw5m"], row["cw1h"], row["cread"])
        if not row["floor_priced"]:
            row["floor_usd"] = None
        out.append(row)
    return out


def summary_payload(conn: sqlite3.Connection, project: str | None) -> dict:
    s = report.summary(conn, project)
    t = s["totals"]
    lo, hi = costs.output_token_range(t["out_bytes"])
    est = s["est_output_usd"]
    exact = s["exact"]
    return {
        "scope": project,
        "window_days": s["window_days"],
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spend": {
            "floor_usd": s["floor_usd"],
            "floor_priced": s["floor_priced"],
            "floor_unknown_equiv_tokens": s["floor_unknown_equiv_tokens"],
            "est_output_usd": [est[0], est[1]],
            "est_unknown_bytes": s["est_unknown_bytes"],
            # Wave D: "approximate" until Anthropic's telemetry covers a day.
            # exact_usd covers exact.covered_dates ONLY — the two figures are
            # reported side by side and never summed by the page.
            "confidence": "exact" if exact["mode"] == "exact" else (
                "mixed" if exact["available"] else "approximate"
            ),
            "confidence_label": report.confidence_label(exact),
            "exact_usd": exact["exact_usd"] if exact["available"] else None,
            "exact": exact,
        },
        "tokens": {
            "cread": t["cread"],
            "cw5m": t["cw5m"],
            "cw1h": t["cw1h"],
            "out_bytes": t["out_bytes"],
            "out_tokens_est": [lo, hi],
            "billable_equiv": s["billable_equiv"],
            "cache_hit": s["cache_hit"],
        },
        "totals": {
            "turns": t["turns"],
            "sessions": s["sessions"],
            "sidechain_turns": t["sidechain"],
            "compactions": s["compactions"],
            "precompact_tokens": s["precompact_tokens"],
        },
        "per_model": sorted(s["per_model"], key=lambda m: m["cread"], reverse=True),
        "series": daily_series(conn, project),
    }


def _window_rows(conn: sqlite3.Connection, start: str, end: str) -> dict[str, dict]:
    """Per-project sums from the daily rollups (kept forever, so the prior-30d
    comparison survives per-turn pruning — PRD R5)."""
    rows = conn.execute(
        "SELECT project, SUM(cw5m) cw5m, SUM(cw1h) cw1h, SUM(cread) cread, "
        "SUM(out_bytes) out_bytes, SUM(turns) turns, SUM(floor_usd) floor_usd, "
        "SUM(CASE WHEN floor_usd IS NULL THEN 1 ELSE 0 END) unpriced_days "
        "FROM daily WHERE date >= ? AND date <= ? GROUP BY project",
        (start, end),
    ).fetchall()
    out = {}
    for r in rows:
        equiv = costs.billable_input_equivalent(r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0)
        out[r["project"]] = {
            "cw5m": r["cw5m"] or 0,
            "cw1h": r["cw1h"] or 0,
            "cread": r["cread"] or 0,
            "out_bytes": r["out_bytes"] or 0,
            "turns": r["turns"] or 0,
            "equiv": equiv,
            "floor_usd": None if r["unpriced_days"] else (r["floor_usd"] or 0.0),
        }
    return out


# Project keys under a system temp dir (pytest tmp_path, /tmp scratch runs) are
# not real repos and clutter the per-project table. Sanitized-key prefixes
# match how Claude Code encodes the OS temp dir path, e.g.
# "/private/var/folders/xx/yyyy/T/pytest-of-you/..." -> "-private-var-folders-...".
# Cosmetic only: these sessions still count in the global spend total.
_EPHEMERAL_PROJECT_PREFIXES = ("-private-var-folders-", "-var-folders-", "-tmp-")


def _is_ephemeral_project(key: str) -> bool:
    return key.startswith(_EPHEMERAL_PROJECT_PREFIXES)


def projects_payload(conn: sqlite3.Connection) -> dict:
    import datetime as _dt

    today = _dt.date.today()
    cur_start = (today - _dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    prev_end = (today - _dt.timedelta(days=WINDOW_DAYS)).isoformat()
    prev_start = (today - _dt.timedelta(days=2 * WINDOW_DAYS - 1)).isoformat()

    cur = _window_rows(conn, cur_start, today.isoformat())
    prev = _window_rows(conn, prev_start, prev_end)

    top_leak: dict[str, dict] = {}
    for f in detectors.rank(store.load_findings(conn, None)):
        p = f["project"]
        if p and p not in top_leak:
            try:
                ev = json.loads(f["evidence"] or "{}")
            except (TypeError, ValueError):
                ev = {}
            top_leak[p] = {
                "detector": f["detector"],
                "name": ev.get("detector_name", f"detector {f['detector']}"),
                "tokens_saved": f["tokens_saved"],
                "usd_saved": f["usd_saved"],
                "effort": f["effort"],
            }

    cwds = store.project_cwds(conn)
    rows = []
    eph = {"floor_usd": 0.0, "floor_known": True, "equiv": 0, "cread": 0,
           "cw5m": 0, "cw1h": 0, "turns": 0, "count": 0}
    for project, c in cur.items():
        if _is_ephemeral_project(project):
            # grouped into one "test & scratch runs" row instead of 40 tmp-dir
            # rows; the transcripts carry no pointer to the repo that spawned
            # them, so honest grouping stops here (no per-repo attribution).
            eph["count"] += 1
            eph["equiv"] += c["equiv"]
            eph["cread"] += c["cread"]
            eph["cw5m"] += c["cw5m"]
            eph["cw1h"] += c["cw1h"]
            eph["turns"] += c["turns"]
            if c["floor_usd"] is None:
                eph["floor_known"] = False
            else:
                eph["floor_usd"] += c["floor_usd"]
            continue
        p = prev.get(project)
        delta_pct = None
        if p and p["equiv"] > 0:
            delta_pct = 100.0 * (c["equiv"] - p["equiv"]) / p["equiv"]
        rows.append(
            {
                "project": project,
                "cwd": cwds.get(project),
                "floor_usd": c["floor_usd"],
                "equiv": c["equiv"],
                "cread": c["cread"],
                "cw5m": c["cw5m"],
                "cw1h": c["cw1h"],
                "turns": c["turns"],
                "cache_hit": costs.cache_hit_ratio(c["cread"], c["cw5m"] + c["cw1h"]),
                "prior_equiv": p["equiv"] if p else None,
                "prior_floor_usd": p["floor_usd"] if p else None,
                "delta_pct": delta_pct,
                "top_leak": top_leak.get(project),
            }
        )
    rows.sort(key=lambda r: r["equiv"], reverse=True)
    if eph["count"]:
        rows.append(
            {
                "project": None,
                "ephemeral": True,
                "eph_count": eph["count"],
                "cwd": None,
                "floor_usd": eph["floor_usd"] if eph["floor_known"] else None,
                "equiv": eph["equiv"],
                "cread": eph["cread"],
                "cw5m": eph["cw5m"],
                "cw1h": eph["cw1h"],
                "turns": eph["turns"],
                "cache_hit": costs.cache_hit_ratio(eph["cread"], eph["cw5m"] + eph["cw1h"]),
                "prior_equiv": None,
                "prior_floor_usd": None,
                "delta_pct": None,
                "top_leak": None,
            }
        )
    return {
        "window_days": WINDOW_DAYS,
        "window": [cur_start, today.isoformat()],
        "prior_window": [prev_start, prev_end],
        "rows": rows,
    }


def findings_payload(conn: sqlite3.Connection, project: str | None) -> dict:
    found = detectors.rank(store.load_findings(conn, project))
    labels = {1: "paste", 3: "habit", 10: "restructure"}
    cwds = store.project_cwds(conn)
    out = []
    for f in found:
        try:
            ev = json.loads(f["evidence"] or "{}")
        except (TypeError, ValueError):
            ev = {}
        out.append(
            {
                "detector": f["detector"],
                "name": ev.get("detector_name", f"detector {f['detector']}"),
                "project": f["project"],
                "cwd": cwds.get(f["project"]),
                "period": f["period"],
                "tokens_saved": f["tokens_saved"],
                "usd_saved": f["usd_saved"],
                "effort": f["effort"],
                "effort_label": labels.get(f["effort"], str(f["effort"])),
                "fix_text": f["fix_text"],
                "evidence": ev,
            }
        )
    return {"scope": project, "count": len(out), "findings": out}


def meta_payload(conn: sqlite3.Connection) -> dict:
    from .. import otel

    p = store.db_path()
    # The page cannot probe the receiver itself: a fetch from the dash origin to
    # port 4318 is cross-origin and the page's own CSP (connect-src 'self')
    # forbids it. So the server does the loopback TCP probe and reports it here.
    return {
        "otel": {
            "receiver_live": otel.receiver_live(),
            "port": otel.DEFAULT_PORT,
            **store.otel_stats(conn),
        },
        "version": __version__,
        "db_path": str(p),
        "db_bytes": p.stat().st_size if p.exists() else None,
        "last_ingest": store.get_meta(conn, "last_ingest"),
        "corpus_files": store.get_meta(conn, "corpus_files"),
        "schema_version": store.get_meta(conn, "schema_version"),
        "constants_version": store.get_meta(conn, "constants_version"),
        "window_days": WINDOW_DAYS,
        "poll_seconds": live.POLL_SECONDS,
    }


# --- http -------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"ccmetrics/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # one terse line, no message content
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Local-only page, no external requests: lock that in.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "img-src data:; connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, default=_default).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        started = time.time()
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        project = (query.get("project") or [None])[0]
        try:
            conn = _conn()
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path == "/api/summary":
                self._json(summary_payload(conn, None))
            elif path == "/api/projects":
                self._json(projects_payload(conn))
            elif path.startswith("/api/project/"):
                key = unquote(path[len("/api/project/"):])
                if not key:
                    self._json({"error": "project key required"}, 404)
                else:
                    self._json(summary_payload(conn, key))
            elif path == "/api/findings":
                self._json(findings_payload(conn, project))
            elif path == "/api/live":
                self._json(live.tiles(conn, project))
            elif path == "/api/constants":
                self._json({"constants": constants.provenance()})
            elif path == "/api/meta":
                self._json(meta_payload(conn))
            elif path == "/favicon.ico":
                self._send(b"", "image/x-icon", 404)
            else:
                self._json({"error": "not found", "path": path}, 404)
        except BrokenPipeError:
            return
        except Exception as exc:  # never leak a traceback into the page
            self._json({"error": exc.__class__.__name__, "detail": str(exc)[:200]}, 500)
        finally:
            ms = (time.time() - started) * 1000
            if ms > 200:
                print(f"ccmetrics dash: {path} took {ms:.0f}ms", flush=True)

    def _static(self, name: str) -> None:
        p = STATIC_DIR / name
        if not p.exists():
            self._json({"error": "missing static file", "file": name}, 404)
            return
        self._send(p.read_bytes(), "text/html; charset=utf-8")


def _default(o):
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> int:
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    httpd.daemon_threads = True
    url = f"http://{HOST}:{port}/"
    print(f"ccmetrics dash: {url}  (ctrl-c to stop)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("", flush=True)
    finally:
        httpd.server_close()
    return 0
