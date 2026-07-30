"""Leak detectors — wave B (PRD R3 + R4b).

Twelve detectors, all metadata-only: they read the SQLite store (counts, byte
sizes, timestamps, tool names, paths, digests) and never touch a JSONL file or
any message text. Running them is a pure function of the store, so ingest reruns
them and replaces the previous finding set.

Three rules bind every detector (PRD R3):

  * metadata-only — evidence JSON carries counts, ids and paths, nothing else;
  * quantified — every finding states tokens saved and the arithmetic behind it;
  * thresholded — every number comes from constants.DETECTOR_THRESHOLDS, each
    entry carrying a source URL or the literal "derived" plus its rule.

Units: `tokens_saved` is always BILLABLE-EQUIVALENT input tokens — cache tokens
folded through the R4 multipliers (write-5m 1.25x, write-1h 2x, read 0.1x) — so
one number is comparable across detectors and `usd_saved = tokens_saved * base
input rate / 1e6` holds everywhere. Raw token/byte counts stay in the evidence.
`usd_saved` is NULL whenever any model involved has no rate in constants.py.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
from bisect import insort as _insort

from . import constants, costs

# --- effort tiers, assigned per fix TYPE, never guessed per finding (R4b) ----

_EFFORT = {name: constants.value(e) for name, e in constants.EFFORT_TIERS.items()}

DETECTOR_NAMES = {
    1: "Cache-miss on idle gaps",
    2: "Compaction tax",
    3: "Context bloat",
    4: "Cache thrash",
    5: "Premium model on small turns",
    6: "Repeated / oversized tool results",
    7: "Hook & denial overhead",
    8: "Unproductive sidechains",
    9: "Agent-team fan-out",
    10: "Phantom idle spend",
    11: "Burn-rate spikes",
    12: "File re-read waste",
}

DETECTOR_EFFORT = {
    1: "paste",
    2: "habit",
    3: "restructure",
    4: "habit",
    5: "paste",
    6: "habit",
    7: "paste",
    8: "habit",
    9: "restructure",
    10: "habit",
    11: "habit",
    12: "paste",
}

# --- fix templates: static strings with numeric slots, filled from the
# finding's own numbers. Never generated per finding (PRD R4b). --------------

FIX_TEMPLATES = {
    1: (
        "CLAUDE.md line — session hygiene:\n"
        '  "After a break longer than {ttl_min} minutes, run /compact before '
        'stepping away and start a fresh session on return."\n'
        "Why: the prompt cache expires after {ttl_min} min. {hits} times in {days}d "
        "this project came back cold and re-wrote {raw_tokens} tokens of context "
        "that would otherwise have been cache reads."
    ),
    2: (
        "Habit — compact on your terms:\n"
        "  Run /compact at a natural break instead of letting auto-compact fire.\n"
        "Why: {sessions} session(s) compacted {compactions} times, re-establishing "
        "{raw_tokens} pre-compaction tokens. Each auto-compact rebuilds the whole "
        "working context; a deliberate one carries only what you still need."
    ),
    3: (
        "Restructure — one session per task:\n"
        "  Split long sessions; start a new one when the task changes.\n"
        "Why: {sessions} session(s) grew cache-read-per-turn faster than this "
        "project's own p90 slope ({p90_slope} tokens/turn), accumulating "
        "{raw_tokens} tokens of extra context re-reads."
    ),
    4: (
        "Habit — keep related work in one warm session:\n"
        "  Batch the work that shares context; avoid one-shot sessions that write "
        "a cache nobody reads.\n"
        "Why: {sessions} session(s) wrote {write_tokens} cache tokens and read back "
        "only {read_tokens}. Breakeven is {breakeven} read(s) per write."
    ),
    5: (
        "settings.json fragment — route small turns to a cheap model:\n"
        '  {{ "model": "{cheap_model}" }}\n'
        "  (or start those turns with /model {cheap_model})\n"
        "Why: {turns} turn(s) on {models} had prompt size, tool-call count and "
        "sidechain depth all at or below this project's p25. Same cache fields at "
        "{cheap_model} rates instead."
    ),
    6: (
        "CLAUDE.md line — do not pay twice for the same result:\n"
        '  "Re-use results already in the transcript; never re-run an identical '
        'command or search twice in one session."\n'
        "Why: {digests} identical tool call(s) ran {calls} times across {sessions} "
        "session(s), re-adding {raw_tokens} tokens of results the transcript "
        "already held."
    ),
    7: (
        "settings.json fragment — stop paying for blocked calls:\n"
        '  {{ "permissions": {{ "allow": [{allow_list}] }} }}\n'
        "Why: {denials} denial(s), {errors} failed tool result(s) and {hook_errors} "
        "hook error(s) burned {raw_tokens} tokens that produced nothing."
    ),
    8: (
        "Habit — brief your sub-agents:\n"
        '  Ask research sub-agents for "paths and line numbers only, no file '
        'bodies" and give them a turn budget.\n'
        "Why: {sessions} session(s) spent {turns} sidechain turns without a single "
        "edit-class tool call."
    ),
    9: (
        "Restructure — cap the fan-out:\n"
        "  Run at most {baseline} sub-agents at once, then a second wave.\n"
        "Why: {turns} turn(s) spawned {max_fanout} sub-agents in one go; the "
        "expected plan-mode baseline is {baseline}."
    ),
    10: (
        "Line item (not a headline) — turns with no prompt in front of them:\n"
        "  {turns} turn(s) ran without a preceding user prompt (session resumes, "
        "status checks), costing {tokens} billable-equivalent tokens.\n"
        "Nothing to fix if you resume sessions on purpose; listed so the number is "
        "never mistaken for work you asked for."
    ),
    11: (
        "Habit — watch the hot turns:\n"
        "  When a turn costs several times the ones before it, stop and start a "
        "fresh session rather than growing the same one.\n"
        "Why: {turns} turn(s) in {sessions} session(s) burned above their own "
        "session's p90, {tokens} billable-equivalent tokens above baseline."
    ),
    12: (
        "CLAUDE.md line — read once:\n"
        '  "Read a file once per session; rely on the transcript instead of '
        're-reading unchanged files."\n'
        "Why: {paths} file path(s) were read {reads} times with no edit in "
        "between, re-adding {raw_tokens} tokens."
    ),
}


# --- small helpers ----------------------------------------------------------


def _t(name):
    return constants.value(constants.DETECTOR_THRESHOLDS[name])


def _threshold_ref(*names) -> list[dict]:
    """The threshold entries a finding crossed, with their sources (R4b)."""
    out = []
    for n in names:
        e = constants.DETECTOR_THRESHOLDS[n]
        out.append({"name": n, "value": e["value"], "source_url": e["source_url"], "as_of": e["as_of"]})
    return out


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile (q in 0..100) over an unsorted list."""
    if not values:
        return None
    return percentile_sorted(sorted(values), q)


def percentile_sorted(vals: list[float], q: float) -> float | None:
    """Same, for a list the caller already keeps sorted (detector 11's window)."""
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    pos = (len(vals) - 1) * (q / 100.0)
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    frac = pos - lo
    return float(vals[lo] + (vals[hi] - vals[lo]) * frac)


def _parse_ts(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _equiv(cw5m: int, cw1h: int, cread: int) -> float:
    return costs.billable_input_equivalent(cw5m or 0, cw1h or 0, cread or 0)


def _bytes_to_tokens(nbytes: float) -> float:
    return (nbytes or 0) / constants.value(constants.BYTES_PER_TOKEN_NOMINAL)


BYTES_PER_TOKEN_SOURCE = {
    "name": "BYTES_PER_TOKEN_NOMINAL",
    "value": constants.value(constants.BYTES_PER_TOKEN_NOMINAL),
    "source_url": constants.BYTES_PER_TOKEN_NOMINAL["source_url"],
}

MULT_SOURCE = {
    "write_5m": constants.value(constants.CACHE_MULTIPLIERS["write_5m"]),
    "write_1h": constants.value(constants.CACHE_MULTIPLIERS["write_1h"]),
    "read": constants.value(constants.CACHE_MULTIPLIERS["read"]),
    "source_url": constants.CACHE_MULTIPLIERS["read"]["source_url"],
}


class _Rates:
    """Per-(model, date) input rate cache, plus the 'any unknown => NULL' rule."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str | None, str], float | None] = {}
        self.unknown_models: set[str] = set()

    def rate(self, model: str | None, ts: str | None) -> float | None:
        key = (model, (ts or "")[:10])
        if key not in self._cache:
            r = constants.model_rates(model, ts)[0]
            self._cache[key] = r
            if r is None:
                self.unknown_models.add(str(model))
        return self._cache[key]

    def usd(self, equiv_tokens: float, model: str | None, ts: str | None) -> float | None:
        r = self.rate(model, ts)
        if r is None:
            return None
        return equiv_tokens * r / costs.PER_MILLION

    def cheapest(self, models_ts: list[tuple[str | None, str | None]]) -> float | None:
        """Cheapest known rate across a set of (model, ts). None if ANY unknown."""
        rates = []
        for model, ts in models_ts:
            r = self.rate(model, ts)
            if r is None:
                return None
            rates.append(r)
        return min(rates) if rates else None


# --- the loaded store snapshot ----------------------------------------------


class Ctx:
    """Everything the detectors need, read once."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.rates = _Rates()
        self.turns = [
            dict(r)
            for r in conn.execute(
                "SELECT id, session_id, project, ts, model, cw5m, cw1h, cread, "
                "out_bytes, sidechain, prompt_bytes FROM turns ORDER BY session_id, ts, id"
            )
        ]
        self.by_id = {t["id"]: t for t in self.turns}
        self.tool_calls = [
            dict(r)
            for r in conn.execute(
                "SELECT turn_id, tool, input_digest, result_bytes, file_path, is_edit, "
                "is_error, denied FROM tool_calls ORDER BY turn_id, rowid"
            )
        ]
        self.sessions = [dict(r) for r in conn.execute("SELECT * FROM sessions")]

        # indexes
        self.turns_by_session: dict[str, list[dict]] = {}
        self.turns_by_project: dict[str, list[dict]] = {}
        for t in self.turns:
            self.turns_by_session.setdefault(t["session_id"], []).append(t)
            self.turns_by_project.setdefault(t["project"], []).append(t)
        self.calls_by_turn: dict[int, list[dict]] = {}
        for c in self.tool_calls:
            if c["turn_id"] in self.by_id:
                self.calls_by_turn.setdefault(c["turn_id"], []).append(c)
        self.session_project = {
            sid: rows[0]["project"] for sid, rows in self.turns_by_session.items()
        }
        for s in self.sessions:
            self.session_project.setdefault(s["id"], s["project"])
        self.projects = sorted(self.turns_by_project)
        dates = [t["ts"][:10] for t in self.turns if t["ts"]]
        self.period = f"{min(dates)}..{max(dates)}" if dates else "empty"

    def calls(self, turn_id: int) -> list[dict]:
        return self.calls_by_turn.get(turn_id, [])

    def equiv_of(self, t: dict) -> float:
        return _equiv(t["cw5m"], t["cw1h"], t["cread"])


def _finding(detector, project, ctx, tokens_saved, usd_saved, evidence, fix_slots) -> dict:
    effort_name = DETECTOR_EFFORT[detector]
    evidence = dict(evidence)
    evidence.setdefault("detector_name", DETECTOR_NAMES[detector])
    evidence["multipliers"] = MULT_SOURCE
    return {
        "detector": detector,
        "project": project,
        "period": ctx.period,
        "tokens_saved": int(round(tokens_saved)),
        "usd_saved": usd_saved,
        "effort": _EFFORT[effort_name],
        "evidence": json.dumps(evidence, sort_keys=True),
        "fix_text": FIX_TEMPLATES[detector].format(**fix_slots),
    }


def _fmt_tok(n) -> str:
    return costs.fmt_tokens(n)


# --- 1. cache-miss on idle gap ----------------------------------------------


def d1_idle_gap(ctx: Ctx) -> list[dict]:
    gap_s = _t("d1_idle_gap_seconds")
    min_rewrite = _t("d1_min_rewrite_tokens")
    w5 = MULT_SOURCE["write_5m"]
    w1 = MULT_SOURCE["write_1h"]
    rd = MULT_SOURCE["read"]

    per_project: dict[str, dict] = {}
    for sid, rows in ctx.turns_by_session.items():
        prev_dt = None
        for t in rows:
            now = _parse_ts(t["ts"])
            if prev_dt is not None and now is not None:
                gap = (now - prev_dt).total_seconds()
                rewritten = (t["cw5m"] or 0) + (t["cw1h"] or 0)
                if gap > gap_s and rewritten >= min_rewrite:
                    acc = per_project.setdefault(
                        t["project"],
                        {"hits": 0, "raw": 0, "equiv": 0.0, "usd": 0.0, "unknown": False,
                         "sessions": set(), "turn_ids": []},
                    )
                    # a cold prefix is re-WRITTEN instead of being READ: the
                    # avoidable part is the multiplier difference, per token.
                    excess = (t["cw5m"] or 0) * (w5 - rd) + (t["cw1h"] or 0) * (w1 - rd)
                    acc["hits"] += 1
                    acc["raw"] += rewritten
                    acc["equiv"] += excess
                    acc["sessions"].add(sid)
                    if len(acc["turn_ids"]) < 20:
                        acc["turn_ids"].append(t["id"])
                    usd = ctx.rates.usd(excess, t["model"], t["ts"])
                    if usd is None:
                        acc["unknown"] = True
                    else:
                        acc["usd"] += usd
            if now is not None:
                prev_dt = now

    out = []
    for project, acc in per_project.items():
        evidence = {
            "hits": acc["hits"],
            "sessions": len(acc["sessions"]),
            "turn_ids_sample": acc["turn_ids"],
            "rewritten_tokens": acc["raw"],
            "thresholds": _threshold_ref("d1_idle_gap_seconds", "d1_min_rewrite_tokens"),
            "arithmetic": (
                "for each post-gap turn: cw5m*(1.25-0.1) + cw1h*(2.0-0.1) "
                "billable-equivalent tokens; usd = that * model input rate / 1e6 "
                "at the turn's own timestamp"
            ),
        }
        out.append(
            _finding(
                1, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"ttl_min": int(gap_s // 60), "hits": acc["hits"], "days": 30,
                 "raw_tokens": _fmt_tok(acc["raw"])},
            )
        )
    return out


# --- 2. compaction tax ------------------------------------------------------


def d2_compaction(ctx: Ctx) -> list[dict]:
    min_c = _t("d2_min_compactions")
    w5 = MULT_SOURCE["write_5m"]
    per_project: dict[str, dict] = {}
    for s in ctx.sessions:
        if (s["compactions"] or 0) < min_c or not (s["precompact_tokens"] or 0):
            continue
        project = s["project"]
        acc = per_project.setdefault(
            project,
            {"sessions": [], "compactions": 0, "raw": 0, "equiv": 0.0, "usd": 0.0,
             "unknown": False},
        )
        raw = s["precompact_tokens"] or 0
        equiv = raw * w5  # the context is re-established as a cache write
        acc["sessions"].append(s["id"])
        acc["compactions"] += s["compactions"] or 0
        acc["raw"] += raw
        acc["equiv"] += equiv
        # conservative: cheapest known rate among the models this session used
        models = [(m, s["ended"] or s["started"]) for m in (s["models"] or "").split(",") if m]
        rate = ctx.rates.cheapest(models) if models else None
        if rate is None:
            acc["unknown"] = True
        else:
            acc["usd"] += equiv * rate / costs.PER_MILLION

    out = []
    for project, acc in per_project.items():
        evidence = {
            "session_ids": acc["sessions"][:20],
            "sessions": len(acc["sessions"]),
            "compactions": acc["compactions"],
            "precompact_tokens": acc["raw"],
            "thresholds": _threshold_ref("d2_min_compactions"),
            "rate_basis": "cheapest known input rate among the session's models",
            "arithmetic": (
                "sum(compactMetadata.preTokens) over sessions with >= 2 compactions, "
                "* 1.25 (re-established as a 5m cache write); usd = that * rate / 1e6"
            ),
        }
        out.append(
            _finding(
                2, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"sessions": len(acc["sessions"]), "compactions": acc["compactions"],
                 "raw_tokens": _fmt_tok(acc["raw"])},
            )
        )
    return out


# --- 3. context bloat -------------------------------------------------------


def _slope(ys: list[float]) -> float:
    """Least-squares slope of y against turn index."""
    n = len(ys)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(ys) / n
    num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(ys))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


def d3_context_bloat(ctx: Ctx) -> list[dict]:
    min_turns = _t("d3_min_turns")
    min_sessions = _t("d3_min_sessions")
    q = _t("d3_percentile")
    rd = MULT_SOURCE["read"]

    per_project_slopes: dict[str, list[tuple[str, float, int]]] = {}
    for sid, rows in ctx.turns_by_session.items():
        if len(rows) < min_turns:
            continue
        slope = _slope([float(t["cread"] or 0) for t in rows])
        if slope <= 0:
            continue
        per_project_slopes.setdefault(rows[0]["project"], []).append((sid, slope, len(rows)))

    out = []
    for project, entries in per_project_slopes.items():
        if len(entries) < min_sessions:
            continue
        p90 = percentile([e[1] for e in entries], q)
        flagged = [e for e in entries if e[1] > p90]
        if not flagged:
            continue
        raw = 0.0
        equiv = 0.0
        usd = 0.0
        unknown = False
        for sid, slope, n in flagged:
            # extra reads vs a p90-shaped session: (slope - p90) summed over the
            # session's turn indexes = (slope - p90) * n(n-1)/2
            extra = (slope - p90) * n * (n - 1) / 2.0
            raw += extra
            equiv += extra * rd
            rows = ctx.turns_by_session[sid]
            models = [(t["model"], t["ts"]) for t in rows]
            rate = ctx.rates.cheapest(models)
            if rate is None:
                unknown = True
            else:
                usd += extra * rd * rate / costs.PER_MILLION
        evidence = {
            "session_ids": [e[0] for e in flagged][:20],
            "sessions": len(flagged),
            "sessions_considered": len(entries),
            "project_p90_slope_tokens_per_turn": round(p90, 1),
            "flagged_slopes_tokens_per_turn": [round(e[1], 1) for e in flagged][:20],
            "extra_read_tokens": int(raw),
            "thresholds": _threshold_ref("d3_percentile", "d3_min_turns", "d3_min_sessions"),
            "arithmetic": (
                "per flagged session: (slope - project p90 slope) * n*(n-1)/2 extra "
                "cache-read tokens, * 0.1 read multiplier; usd = that * cheapest "
                "known model rate in the session / 1e6"
            ),
        }
        out.append(
            _finding(
                3, project, ctx, equiv, None if unknown else usd, evidence,
                {"sessions": len(flagged), "p90_slope": int(p90),
                 "raw_tokens": _fmt_tok(raw)},
            )
        )
    return out


# --- 4. cache thrash --------------------------------------------------------


def d4_cache_thrash(ctx: Ctx) -> list[dict]:
    min_turns = _t("d4_min_turns")
    min_write = _t("d4_min_write_tokens")
    be5 = constants.value(constants.CACHE_BREAKEVEN_READS["write_5m"])
    be1 = constants.value(constants.CACHE_BREAKEVEN_READS["write_1h"])
    w5 = MULT_SOURCE["write_5m"]
    w1 = MULT_SOURCE["write_1h"]
    rd = MULT_SOURCE["read"]

    per_project: dict[str, dict] = {}
    for sid, rows in ctx.turns_by_session.items():
        if len(rows) < min_turns:
            continue
        cw5 = sum(t["cw5m"] or 0 for t in rows)
        cw1 = sum(t["cw1h"] or 0 for t in rows)
        cread = sum(t["cread"] or 0 for t in rows)
        cwrite = cw5 + cw1
        if cwrite < min_write:
            continue
        # breakeven reads required for THIS session's write mix
        need = cw5 * be5 + cw1 * be1
        if cread >= need:
            continue
        # tokens written that never earned their premium
        unamortised = cwrite * (1 - cread / need) if need else 0.0
        mix_mult = (cw5 * w5 + cw1 * w1) / cwrite
        equiv = unamortised * (mix_mult - rd)
        project = rows[0]["project"]
        acc = per_project.setdefault(
            project,
            {"sessions": [], "write": 0, "read": 0, "equiv": 0.0, "usd": 0.0, "unknown": False},
        )
        acc["sessions"].append(sid)
        acc["write"] += cwrite
        acc["read"] += cread
        acc["equiv"] += equiv
        rate = ctx.rates.cheapest([(t["model"], t["ts"]) for t in rows])
        if rate is None:
            acc["unknown"] = True
        else:
            acc["usd"] += equiv * rate / costs.PER_MILLION

    out = []
    for project, acc in per_project.items():
        evidence = {
            "session_ids": acc["sessions"][:20],
            "sessions": len(acc["sessions"]),
            "cache_write_tokens": acc["write"],
            "cache_read_tokens": acc["read"],
            "breakeven_reads": {"write_5m": be5, "write_1h": be1,
                                "source_url": constants.CACHE_BREAKEVEN_READS["write_5m"]["source_url"]},
            "thresholds": _threshold_ref("d4_min_turns", "d4_min_write_tokens"),
            "arithmetic": (
                "per session: need = cw5m*2 + cw1h*1 reads to break even; "
                "unamortised = cwrite * (1 - cread/need); tokens = unamortised * "
                "(write-mix multiplier - 0.1); usd = tokens * cheapest known rate / 1e6"
            ),
        }
        out.append(
            _finding(
                4, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"sessions": len(acc["sessions"]), "write_tokens": _fmt_tok(acc["write"]),
                 "read_tokens": _fmt_tok(acc["read"]), "breakeven": be5},
            )
        )
    return out


# --- 5. model mis-routing ---------------------------------------------------


def d5_model_misrouting(ctx: Ctx) -> list[dict]:
    q = _t("d5_percentile")
    min_turns = _t("d5_min_turns")
    cheap_model = _t("d5_cheap_model")

    out = []
    for project, rows in ctx.turns_by_project.items():
        usable = [t for t in rows if t["prompt_bytes"] is not None]
        if len(usable) < min_turns:
            continue
        p25_prompt = percentile([float(t["prompt_bytes"]) for t in usable], q)
        p25_tools = percentile([float(len(ctx.calls(t["id"]))) for t in usable], q)
        p25_side = percentile([float(t["sidechain"] or 0) for t in usable], q)

        hits = []
        equiv_saved = 0.0
        usd_saved = 0.0
        unknown = False
        models: dict[str, int] = {}
        for t in usable:
            if float(t["prompt_bytes"]) > p25_prompt:
                continue
            if float(len(ctx.calls(t["id"]))) > p25_tools:
                continue
            if float(t["sidechain"] or 0) > p25_side:
                continue
            rate = ctx.rates.rate(t["model"], t["ts"])
            cheap_rate = ctx.rates.rate(cheap_model, t["ts"])
            if rate is None or cheap_rate is None:
                unknown = True
                continue
            if rate <= cheap_rate:
                continue  # already on a cheap model: nothing to route
            eq = ctx.equiv_of(t)
            # cost-equivalent tokens: the share of this turn's billable-equivalent
            # tokens whose price disappears at the cheap model's rate
            equiv_saved += eq * (1 - cheap_rate / rate)
            usd_saved += eq * (rate - cheap_rate) / costs.PER_MILLION
            models[str(t["model"])] = models.get(str(t["model"]), 0) + 1
            hits.append(t["id"])
        if not hits:
            continue
        evidence = {
            "turns": len(hits),
            "turn_ids_sample": hits[:20],
            "turns_considered": len(usable),
            "models": models,
            "p25_prompt_bytes": round(p25_prompt, 1),
            "p25_tool_calls": round(p25_tools, 2),
            "p25_sidechain_depth": round(p25_side, 2),
            "routing_target": cheap_model,
            "thresholds": _threshold_ref("d5_percentile", "d5_min_turns", "d5_cheap_model"),
            "arithmetic": (
                "flagged turns have prompt bytes, tool-call count AND sidechain "
                "depth all <= project p25; usd = sum(billable-equivalent tokens * "
                "(model rate - target rate) / 1e6); tokens = the same equivalents "
                "scaled by (1 - target rate / model rate)"
            ),
        }
        out.append(
            _finding(
                5, project, ctx, equiv_saved, None if unknown else usd_saved, evidence,
                {"turns": len(hits), "models": ", ".join(sorted(models)) or "premium models",
                 "cheap_model": cheap_model},
            )
        )
    return out


# --- 6. repeated / oversized tool results -----------------------------------


def d6_repeated_results(ctx: Ctx) -> list[dict]:
    repeat_n = _t("d6_repeat_calls")
    q = _t("d6_percentile")
    w5 = MULT_SOURCE["write_5m"]

    # per-tool p90 result bytes, recorded as the 'oversized' reference (R3)
    by_tool: dict[str, list[float]] = {}
    for c in ctx.tool_calls:
        if c["result_bytes"]:
            by_tool.setdefault(c["tool"], []).append(float(c["result_bytes"]))
    tool_p90 = {tool: percentile(vals, q) for tool, vals in by_tool.items()}

    groups: dict[tuple[str, str], list[dict]] = {}
    for c in ctx.tool_calls:
        turn = ctx.by_id.get(c["turn_id"])
        if not turn or not c["input_digest"]:
            continue
        groups.setdefault((turn["session_id"], c["input_digest"]), []).append(c)

    per_project: dict[str, dict] = {}
    for (sid, digest), calls in groups.items():
        if len(calls) < repeat_n:
            continue
        sized = [c["result_bytes"] for c in calls if c["result_bytes"]]
        if not sized:
            continue
        avg_bytes = sum(sized) / len(sized)
        raw_tokens = (len(calls) - 1) * _bytes_to_tokens(avg_bytes)
        if raw_tokens <= 0:
            continue
        project = ctx.session_project.get(sid, "?")
        acc = per_project.setdefault(
            project,
            {"digests": 0, "calls": 0, "sessions": set(), "raw": 0.0, "equiv": 0.0,
             "usd": 0.0, "unknown": False, "tools": {}, "samples": []},
        )
        acc["digests"] += 1
        acc["calls"] += len(calls)
        acc["sessions"].add(sid)
        acc["raw"] += raw_tokens
        equiv = raw_tokens * w5
        acc["equiv"] += equiv
        tool = calls[0]["tool"]
        acc["tools"][tool] = acc["tools"].get(tool, 0) + len(calls)
        if len(acc["samples"]) < 20:
            acc["samples"].append(
                {"digest": digest, "tool": tool, "calls": len(calls),
                 "avg_result_bytes": int(avg_bytes),
                 "tool_p90_result_bytes": int(tool_p90.get(tool) or 0)}
            )
        rows = ctx.turns_by_session.get(sid, [])
        rate = ctx.rates.cheapest([(t["model"], t["ts"]) for t in rows]) if rows else None
        if rate is None:
            acc["unknown"] = True
        else:
            acc["usd"] += equiv * rate / costs.PER_MILLION

    out = []
    for project, acc in per_project.items():
        evidence = {
            "repeated_digests": acc["digests"],
            "calls": acc["calls"],
            "sessions": len(acc["sessions"]),
            "tools": acc["tools"],
            "samples": acc["samples"],
            "repeated_result_tokens": int(acc["raw"]),
            "bytes_per_token": BYTES_PER_TOKEN_SOURCE,
            "thresholds": _threshold_ref("d6_repeat_calls", "d6_percentile"),
            "arithmetic": (
                "per (session, tool+input digest) seen >= 3 times: "
                "(repeats - 1) * avg result bytes / 4 raw tokens, * 1.25 "
                "(re-entered the context as a cache write); usd = that * cheapest "
                "known rate in the session / 1e6"
            ),
        }
        out.append(
            _finding(
                6, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"digests": acc["digests"], "calls": acc["calls"],
                 "sessions": len(acc["sessions"]), "raw_tokens": _fmt_tok(acc["raw"])},
            )
        )
    return out


# --- 7. hook & denial overhead ----------------------------------------------


def d7_hook_denial(ctx: Ctx) -> list[dict]:
    w5 = MULT_SOURCE["write_5m"]
    per_project: dict[str, dict] = {}

    for c in ctx.tool_calls:
        if not (c["denied"] or c["is_error"]):
            continue
        turn = ctx.by_id.get(c["turn_id"])
        if not turn:
            continue
        acc = per_project.setdefault(
            turn["project"],
            {"denials": 0, "errors": 0, "hook_errors": 0, "prevented": 0, "tools": {},
             "turn_ids": [], "raw": 0.0, "equiv": 0.0, "usd": 0.0, "unknown": False,
             "counted_turns": set()},
        )
        acc["denials"] += 1 if c["denied"] else 0
        acc["errors"] += 1 if (c["is_error"] and not c["denied"]) else 0
        acc["tools"][c["tool"]] = acc["tools"].get(c["tool"], 0) + 1
        # the error/denial payload itself entered the context
        payload = _bytes_to_tokens(c["result_bytes"] or 0)
        acc["raw"] += payload
        equiv = payload * w5
        # plus the cache this turn wrote to carry a call that produced nothing
        if turn["id"] not in acc["counted_turns"]:
            acc["counted_turns"].add(turn["id"])
            equiv += (turn["cw5m"] or 0) * w5 + (turn["cw1h"] or 0) * MULT_SOURCE["write_1h"]
            if len(acc["turn_ids"]) < 20:
                acc["turn_ids"].append(turn["id"])
        acc["equiv"] += equiv
        usd = ctx.rates.usd(equiv, turn["model"], turn["ts"])
        if usd is None:
            acc["unknown"] = True
        else:
            acc["usd"] += usd

    for s in ctx.sessions:
        if not ((s["hook_errors"] or 0) or (s["prevented"] or 0)):
            continue
        acc = per_project.setdefault(
            s["project"],
            {"denials": 0, "errors": 0, "hook_errors": 0, "prevented": 0, "tools": {},
             "turn_ids": [], "raw": 0.0, "equiv": 0.0, "usd": 0.0, "unknown": False,
             "counted_turns": set()},
        )
        acc["hook_errors"] += s["hook_errors"] or 0
        acc["prevented"] += s["prevented"] or 0

    out = []
    for project, acc in per_project.items():
        if not (acc["denials"] or acc["errors"] or acc["hook_errors"] or acc["prevented"]):
            continue
        allow = ", ".join(f'"{t}"' for t in sorted(acc["tools"])[:5]) or '"Bash(git status)"'
        evidence = {
            "denials": acc["denials"],
            "errored_tool_results": acc["errors"],
            "hook_errors": acc["hook_errors"],
            "prevented_continuations": acc["prevented"],
            "tools": acc["tools"],
            "turn_ids_sample": acc["turn_ids"],
            "turns_affected": len(acc["counted_turns"]),
            "error_payload_tokens": int(acc["raw"]),
            "bytes_per_token": BYTES_PER_TOKEN_SOURCE,
            "thresholds": _threshold_ref("d7_min_events"),
            "arithmetic": (
                "per denied/errored tool call: result bytes / 4 tokens * 1.25, plus "
                "once per affected turn its cache-write tokens (cw5m*1.25 + cw1h*2); "
                "usd = that * the turn's model rate / 1e6"
            ),
        }
        out.append(
            _finding(
                7, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"denials": acc["denials"], "errors": acc["errors"],
                 "hook_errors": acc["hook_errors"], "allow_list": allow,
                 "raw_tokens": _fmt_tok(acc["equiv"])},
            )
        )
    return out


# --- 8. unproductive sidechains ---------------------------------------------


def d8_unproductive_sidechains(ctx: Ctx) -> list[dict]:
    min_turns = _t("d8_min_sidechain_turns")
    per_project: dict[str, dict] = {}
    for sid, rows in ctx.turns_by_session.items():
        side = [t for t in rows if t["sidechain"]]
        if len(side) < min_turns:
            continue
        if any(c["is_edit"] for t in side for c in ctx.calls(t["id"])):
            continue
        project = rows[0]["project"]
        acc = per_project.setdefault(
            project,
            {"sessions": [], "turns": 0, "equiv": 0.0, "usd": 0.0, "unknown": False},
        )
        acc["sessions"].append(sid)
        acc["turns"] += len(side)
        for t in side:
            eq = ctx.equiv_of(t)
            acc["equiv"] += eq
            usd = ctx.rates.usd(eq, t["model"], t["ts"])
            if usd is None:
                acc["unknown"] = True
            else:
                acc["usd"] += usd

    out = []
    for project, acc in per_project.items():
        evidence = {
            "session_ids": acc["sessions"][:20],
            "sessions": len(acc["sessions"]),
            "sidechain_turns": acc["turns"],
            "edit_class_calls_in_those_sidechains": 0,
            "thresholds": _threshold_ref("d8_min_sidechain_turns"),
            "arithmetic": (
                "sessions whose sidechain turns (>= 3) contain zero edit-class tool "
                "calls; tokens = billable-equivalent input tokens of those turns; "
                "usd = per-turn tokens * that turn's model rate / 1e6"
            ),
        }
        out.append(
            _finding(
                8, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"sessions": len(acc["sessions"]), "turns": acc["turns"]},
            )
        )
    return out


# --- 9. agent-team fan-out --------------------------------------------------


def d9_fanout(ctx: Ctx) -> list[dict]:
    baseline = _t("d9_expected_fanout")
    per_project: dict[str, dict] = {}
    # per-session cost of a spawned sub-agent, measured from that session
    for sid, rows in ctx.turns_by_session.items():
        task_calls = {}
        for t in rows:
            n = sum(1 for c in ctx.calls(t["id"]) if c["tool"] == "Task")
            if n:
                task_calls[t["id"]] = n
        if not task_calls:
            continue
        total_tasks = sum(task_calls.values())
        side_equiv = sum(ctx.equiv_of(t) for t in rows if t["sidechain"])
        per_task = side_equiv / total_tasks if total_tasks else 0.0
        hot = {tid: n for tid, n in task_calls.items() if n > baseline}
        if not hot:
            continue
        project = rows[0]["project"]
        acc = per_project.setdefault(
            project,
            {"turn_ids": [], "sessions": set(), "max_fanout": 0, "excess": 0,
             "equiv": 0.0, "usd": 0.0, "unknown": False},
        )
        acc["sessions"].add(sid)
        acc["max_fanout"] = max(acc["max_fanout"], max(hot.values()))
        for tid, n in hot.items():
            excess = n - baseline
            acc["excess"] += excess
            eq = excess * per_task
            acc["equiv"] += eq
            turn = ctx.by_id[tid]
            usd = ctx.rates.usd(eq, turn["model"], turn["ts"])
            if usd is None:
                acc["unknown"] = True
            else:
                acc["usd"] += usd
            if len(acc["turn_ids"]) < 20:
                acc["turn_ids"].append(tid)

    out = []
    for project, acc in per_project.items():
        evidence = {
            "turns": len(acc["turn_ids"]),
            "turn_ids_sample": acc["turn_ids"],
            "sessions": len(acc["sessions"]),
            "max_concurrent_subagents": acc["max_fanout"],
            "excess_subagents": acc["excess"],
            "thresholds": _threshold_ref("d9_expected_fanout"),
            "arithmetic": (
                "turns spawning more than 7 Task calls at once; tokens = excess "
                "sub-agents * (that session's sidechain billable-equivalent tokens "
                "/ its total Task calls); usd = tokens * the turn's model rate / 1e6"
            ),
        }
        out.append(
            _finding(
                9, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"turns": len(acc["turn_ids"]), "max_fanout": acc["max_fanout"],
                 "baseline": baseline},
            )
        )
    return out


# --- 10. phantom idle spend -------------------------------------------------


def d10_phantom_idle(ctx: Ctx) -> list[dict]:
    per_project: dict[str, dict] = {}
    for t in ctx.turns:
        if t["prompt_bytes"] is not None:
            continue
        acc = per_project.setdefault(
            t["project"],
            {"turns": 0, "turn_ids": [], "sessions": set(), "equiv": 0.0, "usd": 0.0,
             "unknown": False},
        )
        acc["turns"] += 1
        acc["sessions"].add(t["session_id"])
        if len(acc["turn_ids"]) < 20:
            acc["turn_ids"].append(t["id"])
        eq = ctx.equiv_of(t)
        acc["equiv"] += eq
        usd = ctx.rates.usd(eq, t["model"], t["ts"])
        if usd is None:
            acc["unknown"] = True
        else:
            acc["usd"] += usd

    out = []
    for project, acc in per_project.items():
        evidence = {
            "turns": acc["turns"],
            "turn_ids_sample": acc["turn_ids"],
            "sessions": len(acc["sessions"]),
            "headline": False,
            "thresholds": _threshold_ref("d10_headline"),
            "arithmetic": (
                "turns with no user prompt recorded before them in the transcript; "
                "tokens = their billable-equivalent input tokens; usd = per turn at "
                "that turn's model rate / 1e6"
            ),
        }
        out.append(
            _finding(
                10, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"turns": acc["turns"], "tokens": _fmt_tok(acc["equiv"])},
            )
        )
    return out


# --- 11. burn-rate spike ----------------------------------------------------


def d11_burn_spike(ctx: Ctx) -> list[dict]:
    q = _t("d11_percentile")
    min_prev = _t("d11_min_preceding")
    per_project: dict[str, dict] = {}
    for sid, rows in ctx.turns_by_session.items():
        if len(rows) <= min_prev:
            continue
        history: list[float] = []  # kept sorted: the p90 window is re-read per turn
        seen = 0
        for t in rows:
            eq = ctx.equiv_of(t)
            if seen >= min_prev:
                p90 = percentile_sorted(history, q)
                if p90 is not None and eq > p90:
                    excess = eq - p90
                    project = t["project"]
                    acc = per_project.setdefault(
                        project,
                        {"turns": 0, "turn_ids": [], "sessions": set(), "equiv": 0.0,
                         "usd": 0.0, "unknown": False},
                    )
                    acc["turns"] += 1
                    acc["sessions"].add(sid)
                    if len(acc["turn_ids"]) < 20:
                        acc["turn_ids"].append(t["id"])
                    acc["equiv"] += excess
                    usd = ctx.rates.usd(excess, t["model"], t["ts"])
                    if usd is None:
                        acc["unknown"] = True
                    else:
                        acc["usd"] += usd
            _insort(history, eq)
            seen += 1

    out = []
    for project, acc in per_project.items():
        evidence = {
            "turns": acc["turns"],
            "turn_ids_sample": acc["turn_ids"],
            "sessions": len(acc["sessions"]),
            "thresholds": _threshold_ref("d11_percentile", "d11_min_preceding"),
            "arithmetic": (
                "a turn is hot when its billable-equivalent tokens exceed the p90 of "
                "the >=10 preceding turns of the same session; tokens = sum of the "
                "excess over that p90; usd = excess * the turn's model rate / 1e6"
            ),
        }
        out.append(
            _finding(
                11, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"turns": acc["turns"], "sessions": len(acc["sessions"]),
                 "tokens": _fmt_tok(acc["equiv"])},
            )
        )
    return out


# --- 12. file re-read waste -------------------------------------------------


def d12_file_rereads(ctx: Ctx) -> list[dict]:
    min_reads = _t("d12_reads")
    w5 = MULT_SOURCE["write_5m"]
    read_class = constants.FILE_PATH_TOOLS - constants.EDIT_CLASS_TOOLS

    # ordered call stream per (session, path)
    streams: dict[tuple[str, str], list[dict]] = {}
    for c in ctx.tool_calls:
        if not c["file_path"]:
            continue
        turn = ctx.by_id.get(c["turn_id"])
        if not turn:
            continue
        streams.setdefault((turn["session_id"], c["file_path"]), []).append(c)

    per_project: dict[str, dict] = {}
    for (sid, path), calls in streams.items():
        # a run ends at any edit-class call to the same path
        runs: list[list[dict]] = [[]]
        for c in calls:
            if c["is_edit"] or c["tool"] not in read_class:
                if runs[-1]:
                    runs.append([])
                continue
            runs[-1].append(c)
        project = ctx.session_project.get(sid, "?")
        for run in runs:
            if len(run) < min_reads:
                continue
            sized = [c["result_bytes"] for c in run if c["result_bytes"]]
            if not sized:
                continue
            avg_bytes = sum(sized) / len(sized)
            raw = (len(run) - 1) * _bytes_to_tokens(avg_bytes)
            acc = per_project.setdefault(
                project,
                {"paths": set(), "reads": 0, "sessions": set(), "raw": 0.0, "equiv": 0.0,
                 "usd": 0.0, "unknown": False, "samples": []},
            )
            acc["paths"].add(path)
            acc["reads"] += len(run)
            acc["sessions"].add(sid)
            acc["raw"] += raw
            equiv = raw * w5
            acc["equiv"] += equiv
            if len(acc["samples"]) < 20:
                acc["samples"].append(
                    {"file_path": path, "reads": len(run), "avg_result_bytes": int(avg_bytes)}
                )
            rows = ctx.turns_by_session.get(sid, [])
            rate = ctx.rates.cheapest([(t["model"], t["ts"]) for t in rows]) if rows else None
            if rate is None:
                acc["unknown"] = True
            else:
                acc["usd"] += equiv * rate / costs.PER_MILLION

    out = []
    for project, acc in per_project.items():
        evidence = {
            "paths": len(acc["paths"]),
            "reads": acc["reads"],
            "sessions": len(acc["sessions"]),
            "samples": acc["samples"],
            "reread_tokens": int(acc["raw"]),
            "bytes_per_token": BYTES_PER_TOKEN_SOURCE,
            "thresholds": _threshold_ref("d12_reads"),
            "arithmetic": (
                "per (session, path) run of >= 3 Read-class calls with no edit to "
                "that path in between: (reads - 1) * avg result bytes / 4 raw "
                "tokens, * 1.25 (re-entered the context as a cache write); usd = "
                "that * cheapest known rate in the session / 1e6"
            ),
        }
        out.append(
            _finding(
                12, project, ctx, acc["equiv"], None if acc["unknown"] else acc["usd"], evidence,
                {"paths": len(acc["paths"]), "reads": acc["reads"],
                 "raw_tokens": _fmt_tok(acc["raw"])},
            )
        )
    return out


DETECTORS = {
    1: d1_idle_gap,
    2: d2_compaction,
    3: d3_context_bloat,
    4: d4_cache_thrash,
    5: d5_model_misrouting,
    6: d6_repeated_results,
    7: d7_hook_denial,
    8: d8_unproductive_sidechains,
    9: d9_fanout,
    10: d10_phantom_idle,
    11: d11_burn_spike,
    12: d12_file_rereads,
}


# --- runner + ranking -------------------------------------------------------


def run_all(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    """Every detector over the whole store. One finding per (detector, project).

    A detector that raises is skipped, never fatal: a broken detector must not
    take the report down with it.
    """
    ctx = Ctx(conn)
    findings: list[dict] = []
    for num in sorted(DETECTORS):
        try:
            found = DETECTORS[num](ctx)
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            findings.append(
                {
                    "detector": num,
                    "project": None,
                    "period": ctx.period,
                    "tokens_saved": 0,
                    "usd_saved": None,
                    "effort": _EFFORT[DETECTOR_EFFORT[num]],
                    "evidence": json.dumps(
                        {
                            "error": type(exc).__name__,
                            "error_detail": str(exc)[:200],
                            "detector_name": DETECTOR_NAMES[num],
                        }
                    ),
                    "fix_text": "",
                }
            )
            continue
        findings.extend(found)
    if project:
        findings = [f for f in findings if f["project"] == project]
    return findings


def rank(findings: list[dict], include_line_items: bool = False) -> list[dict]:
    """PRD R4b: rank by tokens saved / effort tier. Line items never headline."""
    ranked = []
    for f in findings:
        if not include_line_items and not _is_headline(f):
            continue
        if not f["tokens_saved"]:
            continue
        ranked.append(f)
    ranked.sort(key=lambda f: f["tokens_saved"] / max(1, f["effort"]), reverse=True)
    return ranked


def _is_headline(f: dict) -> bool:
    try:
        ev = json.loads(f["evidence"]) if isinstance(f["evidence"], str) else f["evidence"]
    except (TypeError, ValueError):
        return True
    return ev.get("headline", True) is not False


def score(f: dict) -> float:
    return f["tokens_saved"] / max(1, f["effort"])


def run_and_store(conn: sqlite3.Connection) -> dict:
    from . import store

    findings = run_all(conn)
    store.replace_findings(conn, findings)
    per_detector: dict[int, int] = {}
    errors: dict[str, str] = {}
    for f in findings:
        per_detector[f["detector"]] = per_detector.get(f["detector"], 0) + 1
        ev = json.loads(f["evidence"])
        if ev.get("error"):
            errors[str(f["detector"])] = f"{ev['error']}: {ev.get('error_detail', '')}"
    return {
        "findings": len(findings),
        "per_detector": {str(k): v for k, v in sorted(per_detector.items())},
        "errors": errors,
    }
