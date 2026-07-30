"""SQLite state store: schema, migrations, rollup upserts, retention.

Nothing here ever holds message text, file bodies, or tool-result bodies —
only counts, byte sizes, timestamps, tool names, paths and digests.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import constants

SCHEMA_VERSION = 1

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
    msg_id     TEXT
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
    is_edit      INTEGER NOT NULL DEFAULT 0
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
    sidechain_turns  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS sessions_project ON sessions (project, ended);

CREATE TABLE IF NOT EXISTS daily (
    project   TEXT NOT NULL,
    date      TEXT NOT NULL,
    floor_usd REAL,
    cw5m      INTEGER NOT NULL DEFAULT 0,
    cw1h      INTEGER NOT NULL DEFAULT 0,
    cread     INTEGER NOT NULL DEFAULT 0,
    out_bytes INTEGER NOT NULL DEFAULT 0,
    turns     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project, date)
);

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
"""


def db_path() -> Path:
    override = os.environ.get("CCMETRICS_DB")
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_DB_SUBPATH


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    current = get_meta(conn, "schema_version")
    if current is None:
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


# --- rollups ----------------------------------------------------------------


def upsert_session(conn: sqlite3.Connection, agg: dict) -> None:
    """Accumulate a session delta. Deltas only ever come from turns that were
    actually inserted, so re-running ingest cannot inflate a session."""
    conn.execute(
        """
        INSERT INTO sessions(id,project,started,ended,turns,cw5m,cw1h,cread,out_bytes,
                             models,compactions,precompact_tokens,sidechain_turns)
        VALUES(:id,:project,:started,:ended,:turns,:cw5m,:cw1h,:cread,:out_bytes,
               :models,:compactions,:precompact_tokens,:sidechain_turns)
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
            sidechain_turns = sessions.sidechain_turns + excluded.sidechain_turns
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


# --- retention (PRD R5) -----------------------------------------------------


def prune(conn: sqlite3.Connection, now_iso: str, turn_days: int | None = None) -> dict:
    """Idempotent retention pass. Rollups are never deleted to reclaim space."""
    import datetime as _dt

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
    return {
        "turns_deleted": turns_deleted,
        "tool_calls_deleted": tools_deleted,
        "sessions_deleted": sessions_deleted,
        "turn_cutoff": turn_cutoff,
    }


def enforce_size_cap(conn: sqlite3.Connection, now_iso: str, path: Path | None = None) -> dict:
    """If the state file exceeds the cap, shorten per-turn retention (never
    rollups) until it fits or the floor of 7 days is reached."""
    p = Path(path) if path else db_path()
    cap = constants.value(constants.RETENTION["db_bytes_cap"])
    steps = []
    for days in (30, 14, 7):
        size = p.stat().st_size if p.exists() else 0
        if size <= cap:
            return {"size": size, "cap": cap, "steps": steps, "over": False}
        prune(conn, now_iso, turn_days=days)
        conn.execute("VACUUM")
        steps.append(days)
    size = p.stat().st_size if p.exists() else 0
    return {"size": size, "cap": cap, "steps": steps, "over": size > cap}
