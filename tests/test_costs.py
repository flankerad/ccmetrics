"""R4 cost arithmetic: floor is cache-fields-only, raw tokens never priced,
unknown model -> NULL (never 0), <synthetic> -> $0, sonnet-5 date-keyed rate."""

from __future__ import annotations

import datetime as _dt

import pytest

from ccmetrics import costs, ingest

from .util import assistant_rec, make_project, session_path, ts_at, write_lines

BASE = _dt.datetime(2026, 7, 20, 12, 0, 0)


def test_floor_arithmetic_exact():
    cw5m, cw1h, cread = 1000, 2000, 3000
    # hand-computed: cw5m*1.25 + cw1h*2 + cread*0.1
    expected_equiv = 1000 * 1.25 + 2000 * 2.0 + 3000 * 0.1
    assert expected_equiv == 5550.0
    assert costs.billable_input_equivalent(cw5m, cw1h, cread) == expected_equiv

    usd = costs.floor_usd("claude-haiku-4-5", cw5m, cw1h, cread, "2026-07-30")
    expected_usd = expected_equiv * 1.00 / 1_000_000  # haiku input rate $1/MTok
    assert usd == pytest.approx(expected_usd, rel=1e-12)
    assert usd == pytest.approx(0.00555, rel=1e-9)


def test_floor_strips_dated_model_suffix():
    usd_plain = costs.floor_usd("claude-haiku-4-5", 1000, 0, 0, "2026-07-30")
    usd_dated = costs.floor_usd("claude-haiku-4-5-20251001", 1000, 0, 0, "2026-07-30")
    assert usd_plain == usd_dated


def test_unknown_model_floor_is_none_never_zero():
    assert costs.floor_usd("totally-unknown-model-xyz", 100, 100, 100) is None
    # even with zero cache tokens, an unknown model must stay None, not 0.0
    assert costs.floor_usd("totally-unknown-model-xyz", 0, 0, 0) is None


def test_synthetic_model_zero_cost():
    assert costs.floor_usd("<synthetic>", 100_000, 100_000, 100_000) == 0.0


def test_sonnet5_date_keyed_rate_switch():
    equiv = costs.billable_input_equivalent(1000, 0, 0)  # 1250
    before = costs.floor_usd("claude-sonnet-5", 1000, 0, 0, "2026-08-15T00:00:00.000Z")
    after = costs.floor_usd("claude-sonnet-5", 1000, 0, 0, "2026-09-15T00:00:00.000Z")
    assert before == pytest.approx(equiv * 2.00 / 1_000_000, rel=1e-12)
    assert after == pytest.approx(equiv * 3.00 / 1_000_000, rel=1e-12)
    assert before != after
    assert after > before


def test_sonnet5_boundary_dates():
    equiv = costs.billable_input_equivalent(1000, 0, 0)
    last_day_old_rate = costs.floor_usd("claude-sonnet-5", 1000, 0, 0, "2026-08-31T23:59:59.000Z")
    first_day_new_rate = costs.floor_usd("claude-sonnet-5", 1000, 0, 0, "2026-09-01T00:00:00.000Z")
    assert last_day_old_rate == pytest.approx(equiv * 2.00 / 1_000_000, rel=1e-12)
    assert first_day_new_rate == pytest.approx(equiv * 3.00 / 1_000_000, rel=1e-12)


def test_raw_input_output_tokens_never_priced(conn, cc_env):
    """A turn with huge raw in/out tokens but zero cache fields must floor at
    $0 -- raw_in/raw_out are stored but never enter the arithmetic."""
    proj = make_project(cc_env["projects_dir"])
    f = session_path(proj)
    write_lines(
        f,
        [
            assistant_rec(
                "sess1", "msg-1", ts_at(BASE, 0), "claude-haiku-4-5",
                cw5m=0, cw1h=0, cread=0, raw_in=5_000_000, raw_out=5_000_000,
            )
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    row = conn.execute("SELECT raw_in, raw_out FROM turns").fetchone()
    assert row["raw_in"] == 5_000_000
    assert row["raw_out"] == 5_000_000

    day = conn.execute("SELECT floor_usd FROM daily").fetchone()
    assert day["floor_usd"] == 0.0  # not NULL (model is known), not derived from raw tokens
