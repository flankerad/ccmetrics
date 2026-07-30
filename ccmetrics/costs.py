"""R4 cost arithmetic.

Hard rule: the floor derives ONLY from the cache fields times a per-model base
rate. usage.input_tokens / usage.output_tokens are stored raw and never priced —
they are unfinalized streaming placeholders (undercount 17-174x,
anthropics/claude-code#28197).

The floor is a documented LOWER BOUND, never a total. The output term is a
separate labelled range and is never summed into the floor.
"""

from __future__ import annotations

from . import constants

_M5 = constants.value(constants.CACHE_MULTIPLIERS["write_5m"])
_M1H = constants.value(constants.CACHE_MULTIPLIERS["write_1h"])
_MREAD = constants.value(constants.CACHE_MULTIPLIERS["read"])

PER_MILLION = 1_000_000.0


def billable_input_equivalent(cw5m: int, cw1h: int, cread: int) -> float:
    """Cache tokens folded into base-input-rate equivalents.

    Model-independent, so it is still reportable when a model's rate is unknown.
    """
    return cw5m * _M5 + cw1h * _M1H + cread * _MREAD


def floor_usd(
    model: str | None, cw5m: int, cw1h: int, cread: int, ts: str | None = None
) -> float | None:
    """Cache-only spend floor for one turn. None when the model rate is unknown.

    `ts` is the turn's own timestamp: a model whose price changed on a known date
    is billed at the rate that was in force then (constants.MODEL_RATES schedule).
    """
    in_rate, _ = constants.model_rates(model, ts)
    if in_rate is None:
        return None
    return billable_input_equivalent(cw5m, cw1h, cread) * in_rate / PER_MILLION


def output_estimate_usd(
    model: str | None, out_bytes: int, ts: str | None = None
) -> tuple[float, float] | None:
    """Bounded output-cost estimate (low, high) from assistant byte count.

    Never added into the floor; always displayed as a labelled range.
    """
    _, out_rate = constants.model_rates(model, ts)
    if out_rate is None:
        return None
    lo_bpt = constants.value(constants.OUTPUT_BYTES_PER_TOKEN["high"])  # more bytes/token -> fewer tokens
    hi_bpt = constants.value(constants.OUTPUT_BYTES_PER_TOKEN["low"])
    lo_tokens = out_bytes / lo_bpt
    hi_tokens = out_bytes / hi_bpt
    return (lo_tokens * out_rate / PER_MILLION, hi_tokens * out_rate / PER_MILLION)


def output_token_range(out_bytes: int) -> tuple[float, float]:
    lo = out_bytes / constants.value(constants.OUTPUT_BYTES_PER_TOKEN["high"])
    hi = out_bytes / constants.value(constants.OUTPUT_BYTES_PER_TOKEN["low"])
    return (lo, hi)


def is_priced(model: str | None, ts: str | None = None) -> bool:
    return constants.model_rates(model, ts)[0] is not None


def input_rate(model: str | None, ts: str | None = None) -> float | None:
    return constants.model_rates(model, ts)[0]


WRITE_5M_MULT = _M5
WRITE_1H_MULT = _M1H
READ_MULT = _MREAD


# --- unit ladder ------------------------------------------------------------


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value == 0:
        return "$0.00"
    if value < 0.01:
        return "<$0.01"
    if value < 1000:
        return f"${value:,.2f}"
    return f"${value:,.0f}"


def fmt_usd_range(rng: tuple[float, float] | None) -> str:
    if rng is None:
        return "unknown"
    lo, hi = rng
    return f"{fmt_usd(lo)}–{fmt_usd(hi)}"


def fmt_tokens(n: float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= cut:
            return f"{n / cut:.1f}{suffix}"
    return f"{n:.0f}"


def fmt_pct(part: float, whole: float) -> str:
    if not whole:
        return "n/a"
    return f"{100.0 * part / whole:.0f}%"


def cache_hit_ratio(cread: int, cwrite: int) -> float | None:
    total = cread + cwrite
    if total <= 0:
        return None
    return cread / total
