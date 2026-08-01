"""SQLite state store: schema, migrations, rollup upserts, retention.

Nothing here ever holds message text, file bodies, or tool-result bodies —
only counts, byte sizes, timestamps, tool names, paths and digests.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import constants

SCHEMA_VERSION = 4

DEFAULT_DB_SUBPATH = Path(".local/share/ccmetrics/state.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path       TEXT PRIMARY KEY,
    project    TEXT NOT NULL,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    offset     INTEGER NOT NULL,
    session_id TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project    TEXT NOT NULL,
    ts         TEXT,
    model      TEXT,
    cw5m       INTEGER NOT NULL DEFAULT 0,
    cw1h       INTEGER NOT NULL DEFAULT 0,
    cread      INTEGER NOT NULL DEFAULT 0,
    raw_in     INTEGER NOT NULL DEFAULT 0,
    raw_out    INTEGER NOT NULL DEFAULT 0,
    out_bytes  INTEGER NOT NULL DEFAULT 0,
    sidechain  INTEGER NOT NULL DEFAULT 0,
    version    TEXT,
    msg_id     TEXT,
    -- v2: byte length of the user prompt that preceded this turn in the same
    -- transcript (the LENGTH, never the text). NULL means no user prompt came
    -- before it -- that is detector 10's phantom-idle signal, not a missing value.
    prompt_bytes INTEGER
);

-- One API response == one turn. Claude Code writes the same message.id on
-- several consecutive JSONL lines (one per content block), each repeating the
-- identical usage object; this index is what stops that from double-counting.
CREATE UNIQUE INDEX IF NOT EXISTS turns_msg ON turns (session_id, msg_id);
CREATE INDEX IF NOT EXISTS turns_project_ts ON turns (project, ts);
CREATE INDEX IF NOT EXISTS turns_session ON turns (session_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    turn_id      INTEGER NOT NULL,
    tool_use_id  TEXT,
    tool         TEXT NOT NULL,
    input_digest TEXT,
    result_bytes INTEGER,
    file_path    TEXT,
    is_edit      INTEGER NOT NULL DEFAULT 0,
    -- v2 (detector 7): the tool_result came back flagged is_error, or the user
    -- rejected the call (record-level toolDenialKind). Counts only.
    is_error     INTEGER NOT NULL DEFAULT 0,
    denied       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS tool_calls_turn ON tool_calls (turn_id);
CREATE UNIQUE INDEX IF NOT EXISTS tool_calls_use_id ON tool_calls (tool_use_id);
CREATE INDEX IF NOT EXISTS tool_calls_tool ON tool_calls (tool);

CREATE TABLE IF NOT EXISTS sessions (
    id               TEXT PRIMARY KEY,
    project          TEXT NOT NULL,
    started          TEXT,
    ended            TEXT,
    turns            INTEGER NOT NULL DEFAULT 0,
    cw5m             INTEGER NOT NULL DEFAULT 0,
    cw1h             INTEGER NOT NULL DEFAULT 0,
    cread            INTEGER NOT NULL DEFAULT 0,
    out_bytes        INTEGER NOT NULL DEFAULT 0,
    models           TEXT,
    compactions      INTEGER NOT NULL DEFAULT 0,
    precompact_tokens INTEGER NOT NULL DEFAULT 0,
    sidechain_turns  INTEGER NOT NULL DEFAULT 0,
    -- v2 (detector 7): stop-hook errors and hook-prevented continuations.
    hook_errors      INTEGER NOT NULL DEFAULT 0,
    prevented        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS sessions_project ON sessions (project, ended);

-- The real working directory behind a sanitized project key, read from the
-- `cwd` field Claude Code writes into every transcript record. The key's own
-- encoding is lossy ('/', '-', '_' all become '-'); this is the only faithful
-- way back to the true path. A path, never contents (metadata-only rule).
CREATE TABLE IF NOT EXISTS projects (
    project TEXT PRIMARY KEY,
    cwd     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily (
    project   TEXT NOT NULL,
    date      TEXT NOT NULL,
    floor_usd REAL,
    cw5m      INTEGER NOT NULL DEFAULT 0,
    cw1h      INTEGER NOT NULL DEFAULT 0,
    cread     INTEGER NOT NULL DEFAULT 0,
    out_bytes INTEGER NOT NULL DEFAULT 0,
    turns     INTEGER NOT NULL DEFAULT 0,
    -- v3 (wave D, OTEL): Anthropic's own per-request cost for this day, summed
    -- from otel_costs. NULL means no telemetry was received for the day -- the
    -- day is a floor figure, never a total.
    exact_usd      REAL,
    exact_events   INTEGER NOT NULL DEFAULT 0,
    -- otel events / JSONL turns for the day. >= OTEL.exact_coverage_min makes
    -- the day "exact-covered"; anything less stays labelled as a floor.
    exact_coverage REAL,
    PRIMARY KEY (project, date)
);

-- v3 (wave D): one row per OTLP `claude_code.api_request` log record. Counts,
-- ids, a model name and a dollar figure -- never a prompt, body or tool result.
-- event_hash is the dedupe key: telemetry exporters retry, and an identical
-- export must never be counted twice (see otel.event_hash for the recipe).
CREATE TABLE IF NOT EXISTS otel_costs (
    event_hash    TEXT PRIMARY KEY,
    session_id    TEXT,
    project       TEXT,
    ts            TEXT,
    model         TEXT,
    cost_usd      REAL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cache_read    INTEGER,
    cache_write   INTEGER,
    request_id    TEXT,
    received      TEXT
);

CREATE INDEX IF NOT EXISTS otel_costs_day ON otel_costs (project, ts);
CREATE INDEX IF NOT EXISTS otel_costs_session ON otel_costs (session_id);

-- v4 (phase 5): plan-limit snapshots fed by `ccmetrics statusline`, the optional
-- hook Claude Code calls with its own session JSON. One row per window per
-- snapshot -- a percentage, a reset time and the session it came from. The
-- fields are whitelisted at the edge (plan.extract); the incoming JSON is never
-- stored wholesale, and it carries no prompt text to begin with.
CREATE TABLE IF NOT EXISTS plan_snapshots (
    ts         TEXT NOT NULL,
    window_key TEXT NOT NULL,
    used_pct   REAL,
    resets_at  TEXT,
    session_id TEXT,
    PRIMARY KEY (ts, window_key)
);

CREATE INDEX IF NOT EXISTS plan_snapshots_window ON plan_snapshots (window_key, ts);

CREATE TABLE IF NOT EXISTS findings (
    id           INTEGER PRIMARY KEY,
    detector     INTEGER NOT NULL,
    project      TEXT,
    period       TEXT,
    tokens_saved INTEGER,
    usd_saved    REAL,
    effort       INTEGER,
    evidence     TEXT,
    fix_text     TEXT
);

CREATE INDEX IF NOT EXISTS findings_project ON findings (project);
"""

# Additive columns, applied in order to stores created by an older build. Every
# migration this project has ever needed is ADD COLUMN plus CREATE TABLE IF NOT
# EXISTS, so an existing DB upgrades in place and keeps its watermarks: no
# re-ingest, no data loss, no rebuild.
_V2_COLUMNS = (
    ("turns", "prompt_bytes", "INTEGER"),
    ("tool_calls", "is_error", "INTEGER NOT NULL DEFAULT 0"),
    ("tool_calls", "denied", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "hook_errors", "INTEGER NOT NULL DEFAULT 0"),
    ("sessions", "prevented", "INTEGER NOT NULL DEFAULT 0"),
)

# v3 (wave D): OTEL exact costs. daily gains three nullable/defaulted columns;
# otel_costs itself arrives via CREATE TABLE IF NOT EXISTS above.
_V3_COLUMNS = (
    ("daily", "exact_usd", "REAL"),
    ("daily", "exact_events", "INTEGER NOT NULL DEFAULT 0"),
    ("daily", "exact_coverage", "REAL"),
)

_ADDITIVE_COLUMNS = _V2_COLUMNS + _V3_COLUMNS


def db_path() -> Path:
    override = os.environ.get("CCMETRICS_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_DB_SUBPATH


def connect(path: Path | None = None, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (and migrate) the store.

    `check_same_thread=False` is only for the OTEL receiver, whose threaded HTTP
    handlers share one writer connection behind one lock (otel._State). Every
    other caller keeps a connection per thread.
    """
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> list[str]:
    added = []
    for table, column, decl in _ADDITIVE_COLUMNS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not cols or column in cols:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        added.append(f"{table}.{column}")
    return added


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    current = get_meta(conn, "schema_version")
    if current is None:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    elif int(current) < SCHEMA_VERSION:
        set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    elif int(current) > SCHEMA_VERSION:
        raise SystemExit(
            f"state db schema v{current} is newer than this build (v{SCHEMA_VERSION}); "
            f"upgrade ccmetrics or point CCMETRICS_DB elsewhere"
        )
    set_meta(conn, "constants_version", str(constants.CONSTANTS_VERSION))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


# --- watermarks -------------------------------------------------------------


def file_watermarks(conn: sqlite3.Connection) -> dict[str, tuple[float, int, int]]:
    return {
        r["path"]: (r["mtime"], r["size"], r["offset"])
        for r in conn.execute("SELECT path, mtime, size, offset FROM files")
    }


def save_watermark(
    conn: sqlite3.Connection,
    path: str,
    project: str,
    mtime: float,
    size: int,
    offset: int,
    session_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO files(path,project,mtime,size,offset,session_id) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size, "
        "offset=excluded.offset, session_id=COALESCE(excluded.session_id, files.session_id)",
        (path, project, mtime, size, offset, session_id),
    )


def project_cwds(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["project"]: r["cwd"] for r in conn.execute("SELECT project, cwd FROM projects")}


def save_project_cwd(conn: sqlite3.Connection, project: str, cwd: str) -> None:
    conn.execute(
        "INSERT INTO projects(project, cwd) VALUES(?,?) "
        "ON CONFLICT(project) DO UPDATE SET cwd=excluded.cwd",
        (project, cwd),
    )


# --- rollups ----------------------------------------------------------------


def upsert_session(conn: sqlite3.Connection, agg: dict) -> None:
    """Accumulate a session delta. Deltas only ever come from turns that were
    actually inserted, so re-running ingest cannot inflate a session."""
    conn.execute(
        """
        INSERT INTO sessions(id,project,started,ended,turns,cw5m,cw1h,cread,out_bytes,
                             models,compactions,precompact_tokens,sidechain_turns,
                             hook_errors,prevented)
        VALUES(:id,:project,:started,:ended,:turns,:cw5m,:cw1h,:cread,:out_bytes,
               :models,:compactions,:precompact_tokens,:sidechain_turns,
               :hook_errors,:prevented)
        ON CONFLICT(id) DO UPDATE SET
            started = MIN(COALESCE(sessions.started, excluded.started), COALESCE(excluded.started, sessions.started)),
            ended   = MAX(COALESCE(sessions.ended,   excluded.ended),   COALESCE(excluded.ended,   sessions.ended)),
            turns   = sessions.turns + excluded.turns,
            cw5m    = sessions.cw5m + excluded.cw5m,
            cw1h    = sessions.cw1h + excluded.cw1h,
            cread   = sessions.cread + excluded.cread,
            out_bytes = sessions.out_bytes + excluded.out_bytes,
            models  = excluded.models,
            compactions = sessions.compactions + excluded.compactions,
            precompact_tokens = sessions.precompact_tokens + excluded.precompact_tokens,
            sidechain_turns = sessions.sidechain_turns + excluded.sidechain_turns,
            hook_errors = sessions.hook_errors + excluded.hook_errors,
            prevented = sessions.prevented + excluded.prevented
        """,
        agg,
    )


def session_models(conn: sqlite3.Connection, session_id: str) -> set[str]:
    row = conn.execute("SELECT models FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not row or not row["models"]:
        return set()
    return {m for m in row["models"].split(",") if m}


def upsert_daily(conn: sqlite3.Connection, agg: dict) -> None:
    """Accumulate a per-project per-day delta.

    floor_usd is NULL for the whole day as soon as one turn that day ran on a
    model with no known rate (plan schema rule). NULL is sticky.
    """
    conn.execute(
        """
        INSERT INTO daily(project,date,floor_usd,cw5m,cw1h,cread,out_bytes,turns)
        VALUES(:project,:date,:floor_usd,:cw5m,:cw1h,:cread,:out_bytes,:turns)
        ON CONFLICT(project,date) DO UPDATE SET
            floor_usd = CASE
                WHEN daily.floor_usd IS NULL OR excluded.floor_usd IS NULL THEN NULL
                ELSE daily.floor_usd + excluded.floor_usd END,
            cw5m  = daily.cw5m + excluded.cw5m,
            cw1h  = daily.cw1h + excluded.cw1h,
            cread = daily.cread + excluded.cread,
            out_bytes = daily.out_bytes + excluded.out_bytes,
            turns = daily.turns + excluded.turns
        """,
        agg,
    )


def reprice_daily(conn: sqlite3.Connection) -> dict:
    """Recompute daily.floor_usd from the retained per-turn rows.

    Run when constants.py changed (a rate filled in or corrected): the daily
    rollup was priced with the OLD table and would otherwise stay stale/NULL
    forever. Only days that still have turn rows can be repriced; older days
    keep whatever they were priced with, since the turns behind them are gone.
    """
    from . import costs

    rows = conn.execute(
        "SELECT project, substr(ts,1,10) d, model, SUM(cw5m) cw5m, SUM(cw1h) cw1h, "
        "SUM(cread) cread FROM turns WHERE ts IS NOT NULL "
        "GROUP BY project, d, model"
    ).fetchall()
    per_day: dict[tuple[str, str], list] = {}
    for r in rows:
        usd = costs.floor_usd(r["model"], r["cw5m"] or 0, r["cw1h"] or 0, r["cread"] or 0, r["d"])
        acc = per_day.setdefault((r["project"], r["d"]), [0.0, False])
        if usd is None:
            acc[1] = True
        else:
            acc[0] += usd
    updated = 0
    for (project, date), (total, unknown) in per_day.items():
        cur = conn.execute(
            "UPDATE daily SET floor_usd=? WHERE project=? AND date=?",
            (None if unknown else total, project, date),
        )
        updated += cur.rowcount
    conn.commit()
    return {"days_repriced": updated, "days_seen": len(per_day)}


# --- OTEL exact costs (wave D) ----------------------------------------------

EXACT_COVERAGE_MIN = constants.value(constants.OTEL["exact_coverage_min"])

_OTEL_COLUMNS = (
    "event_hash", "session_id", "project", "ts", "model", "cost_usd",
    "input_tokens", "output_tokens", "cache_read", "cache_write",
    "request_id", "received",
)


def insert_otel_events(conn: sqlite3.Connection, events: list[dict]) -> int:
    """Store api_request cost events. Returns how many were NEW.

    INSERT OR IGNORE on the event_hash primary key is the whole dedupe story:
    an exporter that retries a batch, or a user who replays a capture, adds
    zero dollars the second time.
    """
    if not events:
        return 0
    rows = []
    for e in events:
        sid = e.get("session_id")
        project = e.get("project")
        if project is None and sid:
            project = project_of_session(conn, sid)
        rows.append(
            tuple(
                [e.get("event_hash"), sid, project, e.get("ts"), e.get("model"),
                 e.get("cost_usd"), e.get("input_tokens"), e.get("output_tokens"),
                 e.get("cache_read"), e.get("cache_write"), e.get("request_id"),
                 e.get("received")]
            )
        )
    before = conn.total_changes
    conn.executemany(
        f"INSERT OR IGNORE INTO otel_costs({','.join(_OTEL_COLUMNS)}) "
        f"VALUES({','.join('?' * len(_OTEL_COLUMNS))})",
        rows,
    )
    conn.commit()
    return conn.total_changes - before


def project_of_session(conn: sqlite3.Connection, session_id: str) -> str | None:
    """Telemetry carries session.id, not a project. The transcript store knows
    which project that session ran in; until it does, the rows sit unattributed
    (project NULL) and are re-resolved on the next refresh."""
    row = conn.execute("SELECT project FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row:
        return row["project"]
    row = conn.execute(
        "SELECT project FROM files WHERE session_id=? LIMIT 1", (session_id,)
    ).fetchone()
    return row["project"] if row else None


def refresh_exact_daily(conn: sqlite3.Connection) -> dict:
    """Recompute daily.exact_usd / exact_events / exact_coverage from otel_costs.

    Cheap and idempotent (the daily table is one row per project-day), so it can
    run after every ingest and after every accepted telemetry batch.
    """
    resolved = conn.execute(
        "UPDATE otel_costs SET project = ("
        "  SELECT s.project FROM sessions s WHERE s.id = otel_costs.session_id"
        ") WHERE project IS NULL AND session_id IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM sessions s WHERE s.id = otel_costs.session_id)"
    ).rowcount

    conn.execute("UPDATE daily SET exact_usd=NULL, exact_events=0, exact_coverage=NULL")
    rows = conn.execute(
        "SELECT project, substr(ts,1,10) d, SUM(cost_usd) usd, COUNT(*) n "
        "FROM otel_costs WHERE project IS NOT NULL AND ts IS NOT NULL "
        "GROUP BY project, d"
    ).fetchall()
    days = 0
    covered = 0
    for r in rows:
        conn.execute(
            "INSERT INTO daily(project,date,exact_usd,exact_events) VALUES(?,?,?,?) "
            "ON CONFLICT(project,date) DO UPDATE SET "
            "exact_usd=excluded.exact_usd, exact_events=excluded.exact_events",
            (r["project"], r["d"], r["usd"], r["n"]),
        )
        days += 1
    # Coverage is events/turns. A day with telemetry but no turn rows behind it
    # (turns pruned, or transcripts not ingested yet) is covered by definition:
    # there is nothing left to be uncovered.
    conn.execute(
        "UPDATE daily SET exact_coverage = CASE "
        "  WHEN exact_events = 0 THEN NULL "
        "  WHEN turns > 0 THEN CAST(exact_events AS REAL) / turns "
        "  ELSE 1.0 END"
    )
    covered = conn.execute(
        "SELECT COUNT(*) FROM daily WHERE exact_coverage >= ?", (EXACT_COVERAGE_MIN,)
    ).fetchone()[0]
    unattributed = conn.execute(
        "SELECT COUNT(*) FROM otel_costs WHERE project IS NULL"
    ).fetchone()[0]
    conn.commit()
    return {
        "days_with_events": days,
        "days_exact_covered": covered,
        "sessions_resolved": resolved,
        "unattributed_events": unattributed,
    }


def otel_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) n, SUM(cost_usd) usd, MIN(ts) first_ts, MAX(ts) last_ts, "
        "COUNT(DISTINCT session_id) sessions FROM otel_costs"
    ).fetchone()
    return {
        "events": row["n"] or 0,
        "cost_usd": row["usd"],
        "sessions": row["sessions"] or 0,
        "first_ts": row["first_ts"],
        "last_ts": row["last_ts"],
        "days_exact_covered": conn.execute(
            "SELECT COUNT(*) FROM daily WHERE exact_coverage >= ?", (EXACT_COVERAGE_MIN,)
        ).fetchone()[0],
    }


# --- plan snapshots (phase 5, statusline hook) ------------------------------

PLAN_RETENTION_DAYS = constants.value(constants.PLAN["retention_days"])
PLAN_MIN_INTERVAL_S = constants.value(constants.PLAN["min_interval_seconds"])


def last_plan_ts(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(ts) t FROM plan_snapshots").fetchone()
    return row["t"] if row else None


def insert_plan_snapshot(
    conn: sqlite3.Connection, ts: str, windows: dict, session_id: str | None = None
) -> int:
    """Store one snapshot: one row per rate-limit window Claude Code reported.

    `windows` is already whitelisted by plan.extract — {name: {used_pct,
    resets_at}} and nothing else. INSERT OR REPLACE keyed on (ts, window) makes
    a re-run with the same payload a no-op rather than a duplicate.
    """
    if not windows:
        return 0
    rows = [
        (ts, name, w.get("used_pct"), w.get("resets_at"), session_id)
        for name, w in sorted(windows.items())
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO plan_snapshots(ts,window_key,used_pct,resets_at,session_id) "
        "VALUES(?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return len(rows)


def latest_plan_windows(conn: sqlite3.Connection) -> dict:
    """The newest snapshot of each window: {window: {used_pct, resets_at, ts}}.

    Each window is carried independently — Claude Code may report one and not
    the other — so each keeps its own timestamp and its own age.
    """
    try:
        rows = conn.execute(
            "SELECT p.window_key, p.used_pct, p.resets_at, p.ts FROM plan_snapshots p "
            "JOIN (SELECT window_key, MAX(ts) mts FROM plan_snapshots GROUP BY window_key) m "
            "ON m.window_key = p.window_key AND m.mts = p.ts"
        ).fetchall()
    except sqlite3.OperationalError:  # store predates the table
        return {}
    return {
        r["window_key"]: {
            "used_pct": r["used_pct"],
            "resets_at": r["resets_at"],
            "ts": r["ts"],
        }
        for r in rows
    }


def plan_window_trend(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Every stored reading per window over the last `days`: {window: [{ts, used_pct}]}.

    A replay of what the statusline hook actually recorded — nothing is derived
    or interpolated, so a window nobody reported simply does not appear. Capped
    at the newest 100 points per window so a chatty hook cannot bloat the page.
    """
    import datetime as _dt

    cutoff = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    try:
        rows = conn.execute(
            "SELECT window_key, ts, used_pct FROM plan_snapshots WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:  # store predates the table
        return {}
    out: dict = {}
    for r in rows:
        out.setdefault(r["window_key"], []).append({"ts": r["ts"], "used_pct": r["used_pct"]})
    return {k: v[-100:] for k, v in out.items()}


def prune_plan_snapshots(conn: sqlite3.Connection, now_iso: str, days: int | None = None) -> int:
    import datetime as _dt

    days = PLAN_RETENTION_DAYS if days is None else days
    now = _dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    cutoff = (now - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur = conn.execute("DELETE FROM plan_snapshots WHERE ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


# --- findings (PRD R3/R4b, wave B) ------------------------------------------


def replace_findings(conn: sqlite3.Connection, findings: list[dict]) -> int:
    """Findings are a pure function of the store, so every run replaces them."""
    conn.execute("DELETE FROM findings")
    conn.executemany(
        "INSERT INTO findings(detector,project,period,tokens_saved,usd_saved,effort,"
        "evidence,fix_text) VALUES(:detector,:project,:period,:tokens_saved,:usd_saved,"
        ":effort,:evidence,:fix_text)",
        findings,
    )
    conn.commit()
    return len(findings)


def load_findings(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    sql = "SELECT * FROM findings"
    args: tuple = ()
    if project:
        sql += " WHERE project = ?"
        args = (project,)
    return [dict(r) for r in conn.execute(sql, args)]


# --- retention (PRD R5) -----------------------------------------------------


def prune(
    conn: sqlite3.Connection,
    now_iso: str,
    turn_days: int | None = None,
    path: Path | None = None,
) -> dict:
    """Idempotent retention pass (PRD R5).

    Per-turn rows older than `turn_days` go, plus the tool_calls that hung off
    them; session rows older than the session horizon go. Daily rollups are
    NEVER touched — they are what the trends are drawn from, and they must
    outlive the turns behind them.

    Idempotent by construction: the cutoffs are absolute timestamps, so a second
    run over an already-pruned store deletes zero rows. The reported byte sizes
    bracket the pass; the file itself only shrinks on VACUUM (see
    enforce_size_cap), so freed space normally shows up as freelist pages first.
    """
    import datetime as _dt

    p = Path(path) if path else db_path()
    size_before = p.stat().st_size if p.exists() else None

    turn_days = turn_days if turn_days is not None else constants.value(
        constants.RETENTION["turn_days"]
    )
    session_days = constants.value(constants.RETENTION["session_days"])
    now = _dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    # Same shape as the corpus timestamps (2026-07-07T20:41:46.723Z) so plain
    # string comparison orders correctly.
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    turn_cutoff = (now - _dt.timedelta(days=turn_days)).strftime(fmt)
    session_cutoff = (now - _dt.timedelta(days=session_days)).strftime(fmt)

    cur = conn.execute("DELETE FROM turns WHERE ts IS NOT NULL AND ts < ?", (turn_cutoff,))
    turns_deleted = cur.rowcount
    cur = conn.execute(
        "DELETE FROM tool_calls WHERE turn_id NOT IN (SELECT id FROM turns)"
    )
    tools_deleted = cur.rowcount
    cur = conn.execute(
        "DELETE FROM sessions WHERE ended IS NOT NULL AND ended < ?", (session_cutoff,)
    )
    sessions_deleted = cur.rowcount
    conn.commit()
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return {
        "turns_deleted": turns_deleted,
        "tool_calls_deleted": tools_deleted,
        "sessions_deleted": sessions_deleted,
        "rollup_rows": conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0],
        "turn_cutoff": turn_cutoff,
        "session_cutoff": session_cutoff,
        "db_bytes_before": size_before,
        "db_bytes_after": p.stat().st_size if p.exists() else None,
        "reclaimable_bytes": page * free,
    }


def enforce_size_cap(conn: sqlite3.Connection, now_iso: str, path: Path | None = None) -> dict:
    """If the state file exceeds the cap, shorten per-turn retention (never
    rollups) until it fits or the floor of 7 days is reached."""
    p = Path(path) if path else db_path()
    cap = constants.value(constants.RETENTION["db_bytes_cap"])
    steps = []
    rollups = conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
    for days in (30, 14, 7):
        size = p.stat().st_size if p.exists() else 0
        if size <= cap:
            return {"size": size, "cap": cap, "steps": steps, "over": False,
                    "rollup_rows": rollups}
        prune(conn, now_iso, turn_days=days, path=p)
        conn.execute("VACUUM")
        steps.append(days)
    size = p.stat().st_size if p.exists() else 0
    # Floor reached: per-turn retention has been shortened as far as the rule
    # allows. Rollups stay whatever happens — reported, never sacrificed.
    return {"size": size, "cap": cap, "steps": steps, "over": size > cap,
            "rollup_rows": conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0]}
