"""Live current-session tiles (PRD R7).

Tails the newest-mtime JSONL under ~/.claude/projects from a per-path byte
watermark and derives four tiles: burn rate, context %, cache-hit ratio and
session floor $. Detector 11's threshold logic rides along as an inline
runaway warning.

Rules this module inherits and does not relax:

  * READ-ONLY against ~/.claude. Files are opened 'rb', read forward, never
    written, created, locked or truncated. A partial trailing line is left
    unconsumed so a mid-write session is never mis-parsed.
  * IN-MEMORY ONLY. Nothing here touches the sqlite store; the historical
    pipeline owns that. State is a process-local dict of byte offsets and
    running counts, so `ccmetrics dash` restarting simply re-reads the file.
  * METADATA ONLY. Counts, byte lengths, timestamps, model names. No message
    text ever leaves this module (none is even kept).
  * Cost trust rules are R4's: the floor is cache-fields-only, output is a
    labelled range, and raw input_tokens is counted for context occupancy but
    never priced.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path

from . import constants, costs, ingest

STALE_SECONDS = constants.value(constants.LIVE["stale_seconds"])
BURN_WINDOW_SECONDS = constants.value(constants.LIVE["burn_window_seconds"])
MIN_BURN_SPAN_SECONDS = constants.value(constants.LIVE["min_burn_span_seconds"])
POLL_SECONDS = constants.value(constants.LIVE["poll_seconds"])

_D11_PERCENTILE = constants.value(constants.DETECTOR_THRESHOLDS["d11_percentile"])
_D11_MIN_PRECEDING = constants.value(constants.DETECTOR_THRESHOLDS["d11_min_preceding"])

NO_SESSION = "no live session"


# --- finding the active file ------------------------------------------------


def find_active(project: str | None = None, projects_dir: Path | None = None,
                now: float | None = None) -> Path | None:
    """Newest-mtime .jsonl under the projects root (or one project), if fresh.

    Returns None when nothing has been written in STALE_SECONDS — "no live
    session" is a real answer, not a failure.
    """
    root = projects_dir or ingest.claude_projects_dir()
    if project:
        root = root / project
    if not root.exists():
        return None
    now = now if now is not None else time.time()
    best: Path | None = None
    best_mtime = 0.0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in filenames:
            if not name.endswith(".jsonl"):
                continue
            p = Path(dirpath) / name
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime > best_mtime:
                best_mtime, best = mtime, p
    if best is None or (now - best_mtime) > STALE_SECONDS:
        return None
    return best


# --- in-memory tail state ---------------------------------------------------


class _State:
    """Running counts for one session file. Process-local, never persisted."""

    __slots__ = (
        "path", "size", "offset", "session_id", "project", "model",
        "turns", "cw5m", "cw1h", "cread", "raw_in", "out_bytes",
        "floor_usd", "floor_unknown", "first_ts", "last_ts", "last_turn",
        "history", "seen_msgs", "recent", "spike",
    )

    def __init__(self, path: Path, project: str) -> None:
        self.path = str(path)
        self.project = project
        self.size = 0
        self.offset = 0
        self.session_id = None
        self.model = None
        self.turns = 0
        self.cw5m = self.cw1h = self.cread = self.raw_in = self.out_bytes = 0
        self.floor_usd = 0.0
        self.floor_unknown = False
        self.first_ts = None
        self.last_ts = None
        self.last_turn = None       # newest turn's own numbers (ctx % tile)
        self.history = []           # sorted per-turn billable-equivalents (d11)
        self.seen_msgs = set()      # (session_id, message.id): usage counted once
        self.recent = []            # (epoch_seconds, equiv, usd|None) for burn
        self.spike = None           # detector 11 inline warning, or None


_STATES: dict[str, _State] = {}


def reset() -> None:
    """Drop all tail state (test/fixture hook)."""
    _STATES.clear()


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile_sorted(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return float(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo))


def _insort(vals: list[float], x: float) -> None:
    import bisect

    bisect.insort(vals, x)


# --- parsing (same contract as ingest, nothing stored) ----------------------


def _consume(st: _State, buf: bytes) -> int:
    """Fold complete lines into the running counts; return bytes consumed."""
    consumed = 0
    for raw in buf.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break  # mid-write line: leave the watermark before it
        consumed += len(raw)
        if b'"usage"' not in raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict) or rec.get("type") != "assistant":
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        session_id = rec.get("sessionId") or rec.get("session_id") or ""
        msg_id = msg.get("id") or rec.get("uuid") or ""
        st.session_id = st.session_id or session_id or None
        ts = rec.get("timestamp")

        # assistant lines repeat per content block with identical usage
        key = (session_id, msg_id)
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        st.out_bytes += len(text.encode("utf-8", "replace"))
        if key in st.seen_msgs:
            continue
        st.seen_msgs.add(key)

        model = msg.get("model")
        cw5m, cw1h = ingest._split_cache_creation(usage)
        cread = int(usage.get("cache_read_input_tokens") or 0)
        raw_in = int(usage.get("input_tokens") or 0)

        st.turns += 1
        st.model = model or st.model
        st.cw5m += cw5m
        st.cw1h += cw1h
        st.cread += cread
        st.raw_in += raw_in
        if ts:
            st.first_ts = ts if st.first_ts is None or ts < st.first_ts else st.first_ts
            st.last_ts = ts if st.last_ts is None or ts > st.last_ts else st.last_ts

        usd = costs.floor_usd(model, cw5m, cw1h, cread, ts)
        if usd is None:
            st.floor_unknown = True
        else:
            st.floor_usd += usd

        equiv = costs.billable_input_equivalent(cw5m, cw1h, cread)

        # detector 11's rule, applied live: this turn burns hot when it exceeds
        # the p90 of its own session's PRECEDING turns.
        st.spike = None
        if len(st.history) >= _D11_MIN_PRECEDING:
            p90 = _percentile_sorted(st.history, _D11_PERCENTILE)
            if p90 and equiv > p90:
                st.spike = {
                    "equiv_tokens": int(equiv),
                    "p90_equiv_tokens": int(p90),
                    "over_by": round(equiv / p90, 2),
                    "preceding_turns": len(st.history),
                    "detector": 11,
                    "threshold": {
                        "percentile": _D11_PERCENTILE,
                        "min_preceding": _D11_MIN_PRECEDING,
                        "source_url": constants.DETECTOR_THRESHOLDS["d11_percentile"]["source_url"],
                    },
                }
        _insort(st.history, equiv)

        st.last_turn = {
            "model": model,
            "cw5m": cw5m,
            "cw1h": cw1h,
            "cread": cread,
            "raw_in": raw_in,
            "ts": ts,
        }
        epoch = _epoch(ts)
        if epoch is not None:
            st.recent.append((epoch, equiv, usd))
            if len(st.recent) > 4000:
                del st.recent[: len(st.recent) - 2000]
    return consumed


def _tail(path: Path, project: str) -> _State:
    """Read the new bytes of `path` into its running state."""
    key = str(path)
    st = _STATES.get(key)
    try:
        size = path.stat().st_size
    except OSError:
        return st or _State(path, project)
    if st is None or size < st.offset:
        st = _State(path, project)  # new file, or truncated/rewritten: start over
        _STATES[key] = st
    if size == st.offset:
        return st
    with open(path, "rb") as fh:
        if st.offset:
            fh.seek(st.offset)
        buf = fh.read()
    st.offset += _consume(st, buf)
    st.size = size
    return st


# --- the four tiles ---------------------------------------------------------


def _burn(st: _State, now: float) -> dict:
    """Trailing-window burn: billable-equivalent tokens/hr and floor $/hr."""
    if not st.recent:
        return {"equiv_tokens_per_hour": None, "floor_usd_per_hour": None,
                "window_seconds": BURN_WINDOW_SECONDS, "turns": 0, "priced": False}
    end = max(st.recent[-1][0], now)
    cutoff = end - BURN_WINDOW_SECONDS
    window = [r for r in st.recent if r[0] >= cutoff]
    if not window:
        window = st.recent[-1:]
    span = max(end - window[0][0], MIN_BURN_SPAN_SECONDS)
    equiv = sum(r[1] for r in window)
    priced = all(r[2] is not None for r in window)
    usd = sum(r[2] for r in window if r[2] is not None)
    hours = span / 3600.0
    return {
        "equiv_tokens_per_hour": equiv / hours,
        "floor_usd_per_hour": (usd / hours) if priced else None,
        "window_seconds": int(span),
        "turns": len(window),
        "priced": priced,
    }


def _context(st: _State) -> dict:
    """Latest turn's input-side occupancy against the model's window.

    Input side = cache read + cache writes + uncached input. raw input_tokens is
    an untrusted COST field (R4) but it is still a count of tokens that occupied
    the window, so it is included here and never priced.
    """
    t = st.last_turn
    if not t:
        return {"tokens": None, "window": None, "pct": None, "model": st.model,
                "reason": "no turn yet"}
    tokens = t["cread"] + t["cw5m"] + t["cw1h"] + t["raw_in"]
    window = constants.context_window(t["model"])
    if not window:
        return {
            "tokens": tokens,
            "window": None,
            "pct": None,
            "model": t["model"],
            "reason": "context window unknown for this model (constants.py)",
        }
    return {"tokens": tokens, "window": window, "pct": 100.0 * tokens / window,
            "model": t["model"], "reason": None}


def _session_tiles(st: _State, now: float) -> dict:
    cwrite = st.cw5m + st.cw1h
    est = costs.output_estimate_usd(st.model, st.out_bytes, st.last_ts)
    return {
        "status": "live",
        "session_id": st.session_id,
        "project": st.project,
        "model": st.model,
        "turns": st.turns,
        "started": st.first_ts,
        "last_turn_at": st.last_ts,
        "age_seconds": int(now - (_epoch(st.last_ts) or now)),
        "burn": _burn(st, now),
        "context": _context(st),
        "cache_hit": costs.cache_hit_ratio(st.cread, cwrite),
        "floor_usd": None if st.floor_unknown else st.floor_usd,
        "floor_priced": not st.floor_unknown,
        "est_output_usd": list(est) if est else None,
        "tokens": {
            "cread": st.cread,
            "cw5m": st.cw5m,
            "cw1h": st.cw1h,
            "raw_in": st.raw_in,
            "out_bytes": st.out_bytes,
            "billable_equiv": costs.billable_input_equivalent(st.cw5m, st.cw1h, st.cread),
        },
        "warning": st.spike,
        "poll_seconds": POLL_SECONDS,
        "confidence": "approximate · JSONL-only",
    }


def tiles(conn=None, project: str | None = None, projects_dir: Path | None = None) -> dict:
    """R7 tile payload. `conn` is accepted and unused: live tiles never read the
    store, they tail the file directly (PRD Q7)."""
    now = time.time()
    path = find_active(project, projects_dir, now=now)
    if path is None:
        return {
            "status": NO_SESSION,
            "project": project,
            "poll_seconds": POLL_SECONDS,
            "stale_after_seconds": STALE_SECONDS,
        }
    root = projects_dir or ingest.claude_projects_dir()
    try:
        proj = ingest.project_of(path, root)
    except ValueError:
        proj = project or "?"
    st = _tail(path, proj)
    if st.turns == 0:
        return {
            "status": NO_SESSION,
            "project": proj,
            "poll_seconds": POLL_SECONDS,
            "stale_after_seconds": STALE_SECONDS,
            "reason": "active file carries no assistant turns yet",
        }
    return _session_tiles(st, now)
