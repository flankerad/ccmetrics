"""Versioned constant lookup.

Every entry is a dict: {"value": ..., "source_url": ..., "as_of": "YYYY-MM-DD"}
plus an optional "note". No dollar amount, multiplier, or threshold may appear
anywhere else in the codebase (PRD R4: "no ungrounded constants anywhere").

Editing this file is the supported way to correct a rate — no code change needed.
A rate whose value is None means UNKNOWN: cost is withheld and displayed as
"unknown", never estimated (PRD R4).
"""

from __future__ import annotations

CONSTANTS_VERSION = 3

PRICING_DOC = "https://platform.claude.com/docs/en/docs/about-claude/pricing"
CACHING_DOC = "https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching"
TOKEN_DOC = "https://docs.anthropic.com/en/docs/about-claude/glossary"
MODELS_DOC = "https://platform.claude.com/docs/en/docs/about-claude/models/overview"


def _e(value, source_url, as_of, note=None):
    entry = {"value": value, "source_url": source_url, "as_of": as_of}
    if note:
        entry["note"] = note
    return entry


# --- cache multipliers (fixed across every model; only the base rate varies) ---

CACHE_MULTIPLIERS = {
    "write_5m": _e(
        1.25,
        PRICING_DOC,
        "2026-07-30",
        "5-minute ephemeral cache write bills at 1.25x the base input rate.",
    ),
    "write_1h": _e(
        2.0,
        PRICING_DOC,
        "2026-07-30",
        "1-hour ephemeral cache write bills at 2x the base input rate.",
    ),
    "read": _e(
        0.1,
        PRICING_DOC,
        "2026-07-30",
        "Cache read bills at 0.1x the base input rate.",
    ),
}

# --- per-model base rates, USD per 1M tokens ---
# value None == unknown. Unknown rates produce floor_usd = NULL, shown as
# "unknown". Filling one in is a one-line edit here.
#
# Observed in this corpus (assistant records carrying message.usage):
#   claude-fable-5, claude-sonnet-5, claude-opus-4-8, claude-opus-5,
#   claude-haiku-4-5-20251001, <synthetic>

_UNKNOWN_NOTE = (
    "Rate not verified against the published price list at build time; left "
    "unknown on purpose rather than guessed from the model family. Fill in "
    "value once confirmed at the source URL."
)

_RATES_AS_OF = "2026-07-30"

# Some models change price on a known date. Such a model carries a "schedule"
# instead of flat input/output entries: a list of periods, each with an
# inclusive "from"/"to" ISO date (None == open ended). The rate is picked by the
# TURN's timestamp, never by wall-clock, so old turns keep the price they were
# actually billed at.
MODEL_RATES = {
    # in/out USD per 1M tokens
    "claude-haiku-4-5": {
        "input": _e(1.00, PRICING_DOC, _RATES_AS_OF, "Claude Haiku 4.5 list price."),
        "output": _e(5.00, PRICING_DOC, _RATES_AS_OF, "Claude Haiku 4.5 list price."),
    },
    "claude-sonnet-5": {
        "schedule": [
            {
                "from": None,
                "to": "2026-08-31",
                "input": _e(
                    2.00,
                    PRICING_DOC,
                    _RATES_AS_OF,
                    "Claude Sonnet 5 introductory input rate, in force through 2026-08-31.",
                ),
                "output": _e(
                    10.00,
                    PRICING_DOC,
                    _RATES_AS_OF,
                    "Claude Sonnet 5 introductory output rate, in force through 2026-08-31.",
                ),
            },
            {
                "from": "2026-09-01",
                "to": None,
                "input": _e(
                    3.00,
                    PRICING_DOC,
                    _RATES_AS_OF,
                    "Claude Sonnet 5 standard input rate, from 2026-09-01.",
                ),
                "output": _e(
                    15.00,
                    PRICING_DOC,
                    _RATES_AS_OF,
                    "Claude Sonnet 5 standard output rate, from 2026-09-01.",
                ),
            },
        ]
    },
    "claude-opus-5": {
        "input": _e(5.00, PRICING_DOC, _RATES_AS_OF, "Claude Opus 5 list price."),
        "output": _e(25.00, PRICING_DOC, _RATES_AS_OF, "Claude Opus 5 list price."),
    },
    "claude-opus-4-8": {
        "input": _e(5.00, PRICING_DOC, _RATES_AS_OF, "Claude Opus 4.8 list price."),
        "output": _e(25.00, PRICING_DOC, _RATES_AS_OF, "Claude Opus 4.8 list price."),
    },
    "claude-fable-5": {
        "input": _e(10.00, PRICING_DOC, _RATES_AS_OF, "Claude Fable 5 list price."),
        "output": _e(50.00, PRICING_DOC, _RATES_AS_OF, "Claude Fable 5 list price."),
    },
    "<synthetic>": {
        "input": _e(
            0.0,
            PRICING_DOC,
            "2026-07-30",
            "Local CLI-generated turn (errors, interrupts). No API call, no charge.",
        ),
        "output": _e(0.0, PRICING_DOC, "2026-07-30", "Local CLI-generated turn."),
    },
}

# --- context windows (PRD R7 tile 2: ctx % = input-side tokens / window) ---
# value None == UNKNOWN: the tile prints "unknown" rather than dividing by a
# guessed window. Filling one in is a one-line edit here, same rule as a rate.

_WINDOW_AS_OF = "2026-07-30"
_WINDOW_UNVERIFIED = (
    "Window not confirmed against the published model table at build time "
    "(the doc was not reachable offline). Left unknown on purpose: ctx % "
    "prints 'unknown' rather than dividing by a guessed number."
)

CONTEXT_WINDOWS = {
    "claude-haiku-4-5": _e(
        200_000,
        MODELS_DOC,
        _WINDOW_AS_OF,
        "200K standard context window. Re-verify at the source URL when online.",
    ),
    "claude-opus-4-8": _e(
        1_000_000,
        MODELS_DOC,
        _WINDOW_AS_OF,
        "1M-token context window per the models overview page (fetched 2026-07-30).",
    ),
    "claude-opus-5": _e(
        1_000_000,
        MODELS_DOC,
        _WINDOW_AS_OF,
        "1M-token context window per the models overview page (fetched 2026-07-30).",
    ),
    "claude-sonnet-5": _e(
        1_000_000,
        MODELS_DOC,
        _WINDOW_AS_OF,
        "1M-token context window per the models overview page (fetched 2026-07-30).",
    ),
    "claude-fable-5": _e(
        1_000_000,
        MODELS_DOC,
        _WINDOW_AS_OF,
        "1M-token context window per the models overview page (fetched 2026-07-30).",
    ),
    "<synthetic>": _e(
        None,
        MODELS_DOC,
        _WINDOW_AS_OF,
        "Local CLI-generated turn: no model, so no window.",
    ),
}

# --- live tiles (PRD R7) ---

_R7 = "PRD-build-a-brand-new.md#R7"

LIVE = {
    "stale_seconds": _e(
        300,
        _R7,
        "2026-07-30",
        "A session file untouched for 5 minutes is not 'happening right now': "
        "the tiles print 'no live session' instead of a stale snapshot.",
    ),
    "burn_window_seconds": _e(
        900,
        "derived",
        "2026-07-30",
        "Burn rate is measured over the trailing 15 minutes of turns, not the "
        "whole session, so an idle hour does not hide a runaway right now. "
        "15 min = 3x the 5-minute cache TTL, the shortest span that still spans "
        "several turns.",
    ),
    "min_burn_span_seconds": _e(
        60,
        "derived",
        "2026-07-30",
        "Floor on the elapsed span used as the burn-rate denominator; without it "
        "two turns one second apart extrapolate to an absurd hourly rate.",
    ),
    "poll_seconds": _e(
        5,
        _R7,
        "2026-07-30",
        "Dashboard poll interval for /api/live. Refresh is per turn; 5s is the "
        "sampling floor, not a streaming channel.",
    ),
}

# --- output-token estimate band (PRD R4 bounded estimate) ---

OUTPUT_BYTES_PER_TOKEN = {
    "low": _e(
        3.2,
        TOKEN_DOC,
        "2026-07-30",
        "Bytes per output token, low end of the band (~4 chars/token nominal, "
        "-20% for code-heavy English). Fewer bytes/token => more tokens => "
        "upper cost bound.",
    ),
    "high": _e(
        4.8,
        TOKEN_DOC,
        "2026-07-30",
        "Bytes per output token, high end of the band (~4 chars/token nominal, "
        "+20%). More bytes/token => fewer tokens => lower cost bound.",
    ),
}

# --- cache economics (fixed constants, not computations) ---

CACHE_BREAKEVEN_READS = {
    "write_5m": _e(
        2,
        CACHING_DOC,
        "2026-07-30",
        "A 5m cache write (1.25x) pays for itself after 2 reads (0.1x each vs 1x).",
    ),
    "write_1h": _e(
        1,
        CACHING_DOC,
        "2026-07-30",
        "A 1h cache write (2x) pays for itself after 1 read.",
    ),
}

CACHE_TTL_SECONDS = {
    "ephemeral_5m": _e(300, CACHING_DOC, "2026-07-30", "5-minute ephemeral cache TTL."),
    "ephemeral_1h": _e(3600, CACHING_DOC, "2026-07-30", "1-hour ephemeral cache TTL."),
}

# --- store limits (PRD R5) ---

RETENTION = {
    "turn_days": _e(30, "PRD-build-a-brand-new.md#R5", "2026-07-30", "Per-turn rows kept 30 days."),
    "session_days": _e(365, "PRD-build-a-brand-new.md#R5", "2026-07-30", "Session rows kept 1 year."),
    "daily_days": _e(None, "PRD-build-a-brand-new.md#R5", "2026-07-30", "Daily rollups kept forever."),
    "db_bytes_cap": _e(
        100 * 1024 * 1024,
        "PRD-build-a-brand-new.md#R5",
        "2026-07-30",
        "State file stays under 100 MB; per-turn retention shortens before rollups are touched.",
    ),
}

# --- effort tiers for finding ranking (PRD R4b) — wave B consumes these ---

EFFORT_TIERS = {
    "paste": _e(1, "PRD-build-a-brand-new.md#R4b", "2026-07-30", "Config or CLAUDE.md fragment."),
    "habit": _e(3, "PRD-build-a-brand-new.md#R4b", "2026-07-30", "Behaviour change."),
    "restructure": _e(10, "PRD-build-a-brand-new.md#R4b", "2026-07-30", "Workflow rework."),
}

# --- token/byte conversion (detector inputs, never a display unit) ----------

BYTES_PER_TOKEN_NOMINAL = _e(
    4.0,
    TOKEN_DOC,
    "2026-07-30",
    "Nominal 4 bytes per token. Used ONLY to turn a measured tool-result byte "
    "size into a token magnitude for detectors 6 and 12; never used to price a "
    "turn (cost comes from the cache fields alone).",
)

# --- detector thresholds (PRD R3: every detector carries a derived constant or
# an explicit baseline-percentile rule; no hand-waved 'high') -----------------
#
# source_url is either a document that establishes the behaviour, or the literal
# string "derived" plus a note giving the derivation rule in full.

PRD = "PRD-build-a-brand-new.md#R3"

DETECTOR_THRESHOLDS = {
    # 1 — cache-miss on idle gap
    "d1_idle_gap_seconds": _e(
        3600,
        CACHING_DOC,
        "2026-07-30",
        "Derived from the 1-hour ephemeral cache TTL: after a gap longer than "
        "the longest TTL the prefix is certainly cold, so the next turn re-writes "
        "context it could have read. Subscription default; API-key-only users "
        "should lower this to the 5m TTL (300).",
    ),
    "d1_min_rewrite_tokens": _e(
        10000,
        "derived",
        "2026-07-30",
        "A post-gap turn only counts as a cache-miss when it re-writes at least "
        "10K tokens; below that the re-write is a small prompt prefix, not a "
        "re-sent working context (10K = the smallest context that costs more to "
        "re-write at 1.25x than a whole extra turn's reads at 0.1x).",
    ),
    # 2 — compaction tax
    "d2_min_compactions": _e(
        2,
        PRD,
        "2026-07-30",
        "Derived (PRD R3): one compaction is normal housekeeping; a session that "
        "compacts twice or more paid the pre-compaction context tax repeatedly.",
    ),
    # 3 — context bloat
    "d3_percentile": _e(
        90,
        PRD,
        "2026-07-30",
        "Baseline percentile: a session's cache-read-per-turn slope is flagged "
        "when it exceeds this project's own trailing-30d p90 slope.",
    ),
    "d3_min_turns": _e(
        10,
        "derived",
        "2026-07-30",
        "A slope needs points: fewer than 10 turns in a session gives a fit "
        "dominated by the first prompt, so short sessions are excluded.",
    ),
    "d3_min_sessions": _e(
        5,
        "derived",
        "2026-07-30",
        "A project needs at least 5 qualifying sessions before its own p90 is a "
        "baseline rather than an artefact of one session.",
    ),
    # 4 — cache thrash
    "d4_min_turns": _e(
        5,
        "derived",
        "2026-07-30",
        "Below 5 turns a session cannot have amortised a cache write, so a "
        "sub-breakeven ratio is expected behaviour, not a leak.",
    ),
    "d4_min_write_tokens": _e(
        100000,
        "derived",
        "2026-07-30",
        "Ignore sessions writing under 100K cache tokens: the un-amortised "
        "portion of a smaller write is under a cent at every rate in this file.",
    ),
    # 5 — model mis-routing
    "d5_percentile": _e(
        25,
        PRD,
        "2026-07-30",
        "Baseline percentile: a turn is 'small' when its prompt bytes, tool-call "
        "count AND sidechain depth are all at or below this project's trailing-30d "
        "p25. All three, never one.",
    ),
    "d5_min_turns": _e(
        20,
        "derived",
        "2026-07-30",
        "A project needs 20 turns before its own p25 means anything.",
    ),
    "d5_cheap_model": _e(
        "claude-haiku-4-5",
        PRICING_DOC,
        "2026-07-30",
        "The routing target used for the saving arithmetic: the cheapest model "
        "with a known rate in this file. Saving = same cache fields at this "
        "model's input rate instead of the premium one.",
    ),
    # 6 — oversized / repeated tool results
    "d6_repeat_calls": _e(
        3,
        PRD,
        "2026-07-30",
        "Derived (PRD R3): an identical tool+input digest issued 3 or more times "
        "in one session re-pays for a result the transcript already holds.",
    ),
    "d6_percentile": _e(
        90,
        PRD,
        "2026-07-30",
        "Per-tool result-byte p90, recorded on every finding as the 'oversized' "
        "reference point for that tool name.",
    ),
    # 7 — hook & denial overhead
    "d7_min_events": _e(
        1,
        PRD,
        "2026-07-30",
        "Derived (PRD R3): any turn that produced a denial, a hook error or an "
        "errored tool result spent tokens for nothing.",
    ),
    # 8 — unproductive sidechains
    "d8_min_sidechain_turns": _e(
        3,
        "derived",
        "2026-07-30",
        "Derived (PRD R3: zero edit-class calls in the sidechain), with a floor of "
        "3 sidechain turns: a one- or two-turn sidechain is a lookup and is "
        "expected to edit nothing.",
    ),
    # 9 — agent-team fan-out
    "d9_expected_fanout": _e(
        7,
        PRD,
        "2026-07-30",
        "Derived (PRD R3): agent teams in plan mode run ~7x the baseline, so "
        "structural fan-out up to 7 concurrent sub-agents in one turn is expected "
        "and only the excess above 7 is counted.",
    ),
    # 10 — phantom idle spend
    "d10_headline": _e(
        False,
        PRD,
        "2026-07-30",
        "Derived (PRD R3): phantom idle spend is reported as a line item and is "
        "never ranked into the headline top 3.",
    ),
    # 11 — burn-rate spike
    "d11_percentile": _e(
        90,
        PRD,
        "2026-07-30",
        "Baseline percentile: a turn burns hot when it exceeds the p90 of its own "
        "session's preceding turns.",
    ),
    "d11_min_preceding": _e(
        10,
        "derived",
        "2026-07-30",
        "At least 10 preceding turns in the same session before a p90 of that "
        "session is meaningful.",
    ),
    # 12 — file re-read waste
    "d12_reads": _e(
        3,
        PRD,
        "2026-07-30",
        "Derived (PRD R3): the same path read 3+ times in one session with no "
        "edit-class call to that path between the reads is a re-read of unchanged "
        "bytes.",
    ),
}


# --- helpers ---

# Tools whose input carries a real file path worth keeping as metadata.
FILE_PATH_TOOLS = frozenset(
    {"Read", "Edit", "MultiEdit", "Write", "NotebookEdit", "NotebookRead"}
)
EDIT_CLASS_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


def value(entry: dict):
    """Unwrap a constant entry."""
    return entry["value"]


def normalize_model(model: str | None) -> str | None:
    """Map a raw message.model string onto a MODEL_RATES key.

    Strips a trailing -YYYYMMDD release date. Anything unrecognised is returned
    as-is so it shows up as an unknown-rate model rather than being coerced.
    """
    if not model:
        return None
    key = model.strip()
    if len(key) > 9 and key[-9] == "-" and key[-8:].isdigit():
        key = key[:-9]
    return key


def _period_for(schedule: list[dict], date: str | None) -> dict | None:
    """The scheduled period covering `date` (YYYY-MM-DD). None date == today."""
    if not date:
        import datetime as _dt

        date = _dt.date.today().isoformat()
    date = date[:10]
    for period in schedule:
        lo, hi = period.get("from"), period.get("to")
        if lo is not None and date < lo:
            continue
        if hi is not None and date > hi:
            continue
        return period
    return None


def model_rate_entries(model: str | None, ts: str | None = None) -> dict | None:
    """The {"input": entry, "output": entry} pair in force at `ts` (ISO ts or date)."""
    entry = MODEL_RATES.get(normalize_model(model))
    if not entry:
        return None
    schedule = entry.get("schedule")
    if schedule:
        return _period_for(schedule, ts)
    return entry


def context_window(model: str | None) -> int | None:
    """Context window in tokens for a model, or None when unknown (R7 tile 2)."""
    entry = CONTEXT_WINDOWS.get(normalize_model(model))
    return entry["value"] if entry else None


def model_rates(model: str | None, ts: str | None = None) -> tuple[float | None, float | None]:
    """(input_rate, output_rate) in USD per 1M tokens at time `ts`; None where unknown.

    `ts` is the turn's own timestamp so a model that changed price is billed at
    the rate that was in force when the turn ran. Omitting it prices at today's
    rate, which is only correct for forward-looking estimates.
    """
    entry = model_rate_entries(model, ts)
    if not entry:
        return (None, None)
    return (entry["input"]["value"], entry["output"]["value"])


def provenance() -> list[dict]:
    """Flat list of every constant with its source, for UI grounding."""
    out = []
    for group, table in (
        ("cache_multiplier", CACHE_MULTIPLIERS),
        ("output_bytes_per_token", OUTPUT_BYTES_PER_TOKEN),
        ("cache_breakeven_reads", CACHE_BREAKEVEN_READS),
        ("cache_ttl_seconds", CACHE_TTL_SECONDS),
        ("retention", RETENTION),
        ("effort_tier", EFFORT_TIERS),
        ("context_window", CONTEXT_WINDOWS),
        ("live", LIVE),
    ):
        for name, entry in table.items():
            out.append({"group": group, "name": name, **entry})
    for name, entry in DETECTOR_THRESHOLDS.items():
        out.append({"group": "detector_threshold", "name": name, **entry})
    for name, entry in MODEL_RATES.items():
        periods = entry.get("schedule") or [entry]
        for period in periods:
            span = ""
            if period.get("from") or period.get("to"):
                span = f" [{period.get('from') or '...'}..{period.get('to') or '...'}]"
            for side in ("input", "output"):
                out.append(
                    {"group": f"model_rate_{side}", "name": name + span, **period[side]}
                )
    return out
