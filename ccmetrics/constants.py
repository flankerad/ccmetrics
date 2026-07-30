"""Versioned constant lookup.

Every entry is a dict: {"value": ..., "source_url": ..., "as_of": "YYYY-MM-DD"}
plus an optional "note". No dollar amount, multiplier, or threshold may appear
anywhere else in the codebase (PRD R4: "no ungrounded constants anywhere").

Editing this file is the supported way to correct a rate — no code change needed.
A rate whose value is None means UNKNOWN: cost is withheld and displayed as
"unknown", never estimated (PRD R4).
"""

from __future__ import annotations

CONSTANTS_VERSION = 1

PRICING_DOC = "https://docs.anthropic.com/en/docs/about-claude/pricing"
CACHING_DOC = "https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching"
TOKEN_DOC = "https://docs.anthropic.com/en/docs/about-claude/glossary"


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

MODEL_RATES = {
    # in/out USD per 1M tokens
    "claude-haiku-4-5": {
        "input": _e(1.00, PRICING_DOC, "2025-10-15", "Claude Haiku 4.5 list price."),
        "output": _e(5.00, PRICING_DOC, "2025-10-15", "Claude Haiku 4.5 list price."),
    },
    "claude-sonnet-5": {
        "input": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
        "output": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
    },
    "claude-opus-5": {
        "input": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
        "output": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
    },
    "claude-opus-4-8": {
        "input": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
        "output": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
    },
    "claude-fable-5": {
        "input": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
        "output": _e(None, PRICING_DOC, "2026-07-30", _UNKNOWN_NOTE),
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

# Detector thresholds land in wave B; the table lives here so no detector ever
# hardcodes a number.
DETECTOR_THRESHOLDS: dict[str, dict] = {}


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


def model_rates(model: str | None) -> tuple[float | None, float | None]:
    """(input_rate, output_rate) in USD per 1M tokens; None where unknown."""
    entry = MODEL_RATES.get(normalize_model(model))
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
    ):
        for name, entry in table.items():
            out.append({"group": group, "name": name, **entry})
    for name, entry in MODEL_RATES.items():
        for side in ("input", "output"):
            out.append({"group": f"model_rate_{side}", "name": name, **entry[side]})
    return out
