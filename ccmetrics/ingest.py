"""Read-only incremental ingest of ~/.claude/projects/**/*.jsonl.

Never writes, creates or locks anything under ~/.claude. Files are opened 'rb'
and read forward from a byte watermark; a live-appending session file is safe
because the watermark only ever advances to the end of the last COMPLETE line.

Field names below were verified against the real corpus (1550 files, 551 MB) on
2026-07-30 — see the module notes where reality differed from the plan:

  * assistant records repeat: Claude Code emits one JSONL line per content
    block, each carrying the SAME message.id and the SAME usage object (832
    lines / 430 distinct messages in the largest file). Usage is therefore
    counted once per (sessionId, message.id) via a unique index; extra lines
    only contribute their own text bytes and tool_use blocks.
  * compaction pre-token count lives at compactMetadata.preTokens (the plan
    guessed preCompactTokenCount; both are accepted, corpus name wins).
  * subagent transcripts live at <project>/<session>/subagents/**.jsonl and
    carry the PARENT sessionId, so they roll into the parent session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from . import constants, costs, store

PROJECTS_DIRNAME = "projects"

# Cheap byte prefilter: a line we care about always contains one of these.
# Everything else (attachments, file-history snapshots, ai-title, mode records)
# is skipped without paying for json.loads. Consequence: `parse_failures`
# counts malformed lines that LOOKED relevant; a malformed line with none of
# these markers is counted under `lines_skipped` instead. Either way, never fatal.
_WANT = (b'"usage"', b'"tool_result"', b"compact_boundary")

_NONWORD = re.compile(r"[^a-zA-Z0-9]")


def claude_projects_dir() -> Path:
    root = os.environ.get("CCMETRICS_CLAUDE_DIR")
    base = Path(root).expanduser() if root else Path.home() / ".claude"
    return base / PROJECTS_DIRNAME


def encode_project(path: str) -> str:
    """Claude Code's project-dir encoding: every non-alphanumeric byte -> '-'.

    Verified against 52 real project dirs vs the cwd recorded inside them: 0
    mismatches.
    """
    return _NONWORD.sub("-", os.path.abspath(path))


def project_of(file_path: Path, projects_dir: Path) -> str:
    rel = file_path.relative_to(projects_dir)
    return rel.parts[0]


def discover(projects_dir: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(projects_dir):
        dirnames.sort()
        for name in sorted(filenames):
            if name.endswith(".jsonl"):
                out.append(Path(dirpath) / name)
    return out


# --- metadata extraction (no bodies, ever) ----------------------------------


def _text_bytes(block: dict) -> int:
    """Byte length of an assistant text block. The text itself is discarded."""
    text = block.get("text")
    if isinstance(text, str):
        return len(text.encode("utf-8", "replace"))
    return 0


def _result_bytes(content) -> int:
    """Byte length of a tool_result payload. Measured, never stored."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8", "replace"))
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    total += len(block["text"].encode("utf-8", "replace"))
                else:
                    # image / other payloads: measure the serialized size only
                    try:
                        total += len(json.dumps(block, separators=(",", ":")))
                    except (TypeError, ValueError):
                        pass
            elif isinstance(block, str):
                total += len(block.encode("utf-8", "replace"))
        return total
    try:
        return len(json.dumps(content, separators=(",", ":")))
    except (TypeError, ValueError):
        return 0


def tool_digest(tool: str, tool_input) -> str:
    """sha256(tool + canonical JSON input)[:16].

    The digest is a fingerprint for repeat detection (detector 6). It is a
    one-way hash; no input text is stored.
    """
    try:
        canon = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canon = ""
    return hashlib.sha256((tool + "\x00" + canon).encode("utf-8", "replace")).hexdigest()[:16]


def _split_cache_creation(usage: dict) -> tuple[int, int]:
    """(5m, 1h) cache-write tokens.

    Corpus always carries usage.cache_creation as a dict with both keys; the
    fallback assigns an unsplit whole to the 5m bucket per the parsing contract.
    """
    cc = usage.get("cache_creation")
    if isinstance(cc, dict):
        five = cc.get("ephemeral_5m_input_tokens") or 0
        hour = cc.get("ephemeral_1h_input_tokens") or 0
        if five or hour:
            return int(five), int(hour)
    whole = usage.get("cache_creation_input_tokens") or 0
    return int(whole), 0


# --- per-run accumulators ---------------------------------------------------


class _Run:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.daily: dict[tuple[str, str], dict] = {}
        self.turns_new = 0
        self.tool_calls_new = 0
        self.parse_failures = 0
        self.lines_examined = 0
        self.lines_skipped = 0
        self.unknown_types: dict[str, int] = {}
        self.files_read = 0
        self.bytes_read = 0
        self.unknown_model_turns = 0
        self.models: dict[str, int] = {}

    def session(self, sid: str, project: str) -> dict:
        agg = self.sessions.get(sid)
        if agg is None:
            agg = {
                "id": sid,
                "project": project,
                "started": None,
                "ended": None,
                "turns": 0,
                "cw5m": 0,
                "cw1h": 0,
                "cread": 0,
                "out_bytes": 0,
                "models": set(),
                "compactions": 0,
                "precompact_tokens": 0,
                "sidechain_turns": 0,
            }
            self.sessions[sid] = agg
        return agg

    def day(self, project: str, date: str) -> dict:
        key = (project, date)
        agg = self.daily.get(key)
        if agg is None:
            agg = {
                "project": project,
                "date": date,
                "floor_usd": 0.0,
                "floor_unknown": False,
                "cw5m": 0,
                "cw1h": 0,
                "cread": 0,
                "out_bytes": 0,
                "turns": 0,
            }
            self.daily[key] = agg
        return agg


def _touch_time(agg: dict, ts: str | None) -> None:
    if not ts:
        return
    if agg["started"] is None or ts < agg["started"]:
        agg["started"] = ts
    if agg["ended"] is None or ts > agg["ended"]:
        agg["ended"] = ts


# --- the line handlers ------------------------------------------------------


def _handle_assistant(conn, rec, project, run, turn_cache) -> None:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return

    session_id = rec.get("sessionId") or rec.get("session_id") or ""
    msg_id = msg.get("id") or rec.get("uuid") or ""
    if not session_id or not msg_id:
        return
    key = (session_id, msg_id)
    model = msg.get("model")
    ts = rec.get("timestamp")

    # text bytes + tool_use blocks belong to THIS line
    out_bytes = 0
    tool_uses = []
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out_bytes += _text_bytes(block)
            elif btype == "tool_use":
                tool_uses.append(block)

    turn_id = turn_cache.get(key)
    fresh = False
    if turn_id is None:
        cw5m, cw1h = _split_cache_creation(usage)
        row = (
            session_id,
            project,
            ts,
            model,
            cw5m,
            cw1h,
            int(usage.get("cache_read_input_tokens") or 0),
            int(usage.get("input_tokens") or 0),
            int(usage.get("output_tokens") or 0),
            0,
            1 if rec.get("isSidechain") else 0,
            rec.get("version"),
            msg_id,
        )
        try:
            cur = conn.execute(
                "INSERT INTO turns(session_id,project,ts,model,cw5m,cw1h,cread,raw_in,"
                "raw_out,out_bytes,sidechain,version,msg_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            turn_id = cur.lastrowid
            fresh = True
        except sqlite3.IntegrityError:
            found = conn.execute(
                "SELECT id FROM turns WHERE session_id=? AND msg_id=?", (session_id, msg_id)
            ).fetchone()
            if found is None:
                return
            turn_id = found["id"]
        turn_cache[key] = turn_id

        if fresh:
            run.turns_new += 1
            run.models[str(model)] = run.models.get(str(model), 0) + 1
            sess = run.session(session_id, project)
            sess["turns"] += 1
            sess["cw5m"] += cw5m
            sess["cw1h"] += cw1h
            sess["cread"] += row[6]
            sess["models"].add(str(model))
            sess["sidechain_turns"] += row[10]
            _touch_time(sess, ts)

            day = run.day(project, (ts or "")[:10] or "unknown")
            day["turns"] += 1
            day["cw5m"] += cw5m
            day["cw1h"] += cw1h
            day["cread"] += row[6]
            usd = costs.floor_usd(model, cw5m, cw1h, row[6])
            if usd is None:
                day["floor_unknown"] = True
                run.unknown_model_turns += 1
            else:
                day["floor_usd"] += usd

    if out_bytes:
        conn.execute(
            "UPDATE turns SET out_bytes = out_bytes + ? WHERE id = ?", (out_bytes, turn_id)
        )
        run.session(session_id, project)["out_bytes"] += out_bytes
        run.day(project, (ts or "")[:10] or "unknown")["out_bytes"] += out_bytes

    for block in tool_uses:
        name = block.get("name") or "?"
        tool_input = block.get("input")
        file_path = None
        if name in constants.FILE_PATH_TOOLS and isinstance(tool_input, dict):
            candidate = tool_input.get("file_path") or tool_input.get("notebook_path")
            if isinstance(candidate, str):
                file_path = candidate
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tool_calls(turn_id,tool_use_id,tool,input_digest,"
                "result_bytes,file_path,is_edit) VALUES(?,?,?,?,?,?,?)",
                (
                    turn_id,
                    block.get("id"),
                    name,
                    tool_digest(name, tool_input),
                    None,
                    file_path,
                    1 if name in constants.EDIT_CLASS_TOOLS else 0,
                ),
            )
            run.tool_calls_new += cur.rowcount
        except sqlite3.IntegrityError:
            pass


def _handle_user(conn, rec, run) -> None:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        use_id = block.get("tool_use_id")
        if not use_id:
            continue
        conn.execute(
            "UPDATE tool_calls SET result_bytes = ? WHERE tool_use_id = ?",
            (_result_bytes(block.get("content")), use_id),
        )


def _handle_system(rec, project, run) -> None:
    if rec.get("subtype") != "compact_boundary":
        return
    session_id = rec.get("sessionId") or rec.get("session_id")
    if not session_id:
        return
    meta = rec.get("compactMetadata")
    pre = 0
    if isinstance(meta, dict):
        # corpus name is preTokens; the plan's guess is kept as a fallback
        for name in ("preTokens", "preCompactTokenCount", "preCompactTokens"):
            v = meta.get(name)
            if isinstance(v, (int, float)):
                pre = int(v)
                break
    sess = run.session(session_id, project)
    sess["compactions"] += 1
    sess["precompact_tokens"] += pre
    _touch_time(sess, rec.get("timestamp"))


# --- file walk --------------------------------------------------------------


def _ingest_file(conn, path: Path, project: str, start_offset: int, run: _Run) -> tuple[int, str | None]:
    offset = start_offset
    session_id = None
    turn_cache: dict[tuple[str, str], int] = {}
    with open(path, "rb") as fh:
        if offset:
            fh.seek(offset)
        buf = fh.read()
    if not buf:
        return offset, session_id

    consumed = 0
    for raw in buf.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            # partial trailing line: a live session is mid-write. Leave the
            # watermark before it so the next run re-reads it whole.
            break
        consumed += len(raw)
        if not any(token in raw for token in _WANT):
            run.lines_skipped += 1
            continue
        run.lines_examined += 1
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            run.parse_failures += 1
            continue
        if not isinstance(rec, dict):
            run.parse_failures += 1
            continue
        rtype = rec.get("type")
        session_id = rec.get("sessionId") or rec.get("session_id") or session_id
        try:
            if rtype == "assistant":
                _handle_assistant(conn, rec, project, run, turn_cache)
            elif rtype == "user":
                _handle_user(conn, rec, run)
            elif rtype == "system":
                _handle_system(rec, project, run)
            else:
                run.unknown_types[str(rtype)] = run.unknown_types.get(str(rtype), 0) + 1
        except sqlite3.DatabaseError:
            raise
        except Exception:
            # schema drift must never be fatal (R1)
            run.parse_failures += 1

    run.bytes_read += consumed
    return offset + consumed, session_id


def ingest(conn: sqlite3.Connection, projects_dir: Path | None = None, verbose: bool = False) -> dict:
    projects_dir = projects_dir or claude_projects_dir()
    started = time.time()
    run = _Run()
    if not projects_dir.exists():
        return _finish(conn, run, started, projects_dir, files_total=0)

    marks = store.file_watermarks(conn)
    files = discover(projects_dir)
    for path in files:
        try:
            st = path.stat()
        except OSError:
            continue
        key = str(path)
        mark = marks.get(key)
        offset = 0
        if mark:
            mtime, size, prev_offset = mark
            if st.st_mtime == mtime and st.st_size == size:
                continue  # untouched
            if st.st_size < prev_offset:
                offset = 0  # truncated / rewritten: start over
            else:
                offset = prev_offset
        project = project_of(path, projects_dir)
        new_offset, session_id = _ingest_file(conn, path, project, offset, run)
        run.files_read += 1
        store.save_watermark(
            conn, key, project, st.st_mtime, st.st_size, new_offset, session_id
        )
        if run.files_read % 200 == 0:
            conn.commit()
    conn.commit()
    return _finish(conn, run, started, projects_dir, files_total=len(files))


def _finish(conn, run: _Run, started: float, projects_dir: Path, files_total: int) -> dict:
    for sid, agg in run.sessions.items():
        merged = store.session_models(conn, sid) | agg["models"]
        payload = dict(agg)
        payload["models"] = ",".join(sorted(m for m in merged if m))
        store.upsert_session(conn, payload)
    for agg in run.daily.values():
        payload = {
            "project": agg["project"],
            "date": agg["date"],
            "floor_usd": None if agg["floor_unknown"] else agg["floor_usd"],
            "cw5m": agg["cw5m"],
            "cw1h": agg["cw1h"],
            "cread": agg["cread"],
            "out_bytes": agg["out_bytes"],
            "turns": agg["turns"],
        }
        store.upsert_daily(conn, payload)
    conn.commit()

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    pruned = store.prune(conn, now_iso)
    capped = store.enforce_size_cap(conn, now_iso)

    store.set_meta(conn, "last_ingest", now_iso)
    store.set_meta(conn, "corpus_dir", str(projects_dir))
    store.set_meta(conn, "corpus_files", str(files_total))
    store.set_meta(conn, "last_parse_failures", str(run.parse_failures))
    conn.commit()

    return {
        "elapsed_s": round(time.time() - started, 3),
        "files_total": files_total,
        "files_read": run.files_read,
        "bytes_read": run.bytes_read,
        "lines_examined": run.lines_examined,
        "lines_skipped": run.lines_skipped,
        "parse_failures": run.parse_failures,
        "turns_new": run.turns_new,
        "tool_calls_new": run.tool_calls_new,
        "sessions_touched": len(run.sessions),
        "unknown_types": run.unknown_types,
        "unknown_model_turns": run.unknown_model_turns,
        "models": run.models,
        "pruned": pruned,
        "size": capped,
    }
