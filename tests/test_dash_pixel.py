"""12-pixel-stories: story tests proving the pixel dash rebuild. Source-presence
guards over the shipped page (reusing test_dash_render.py's page_text fixture)
plus payload-shape checks via the seeded-store fixtures test_dash_api.py
already provides -- no new fixtures.
"""

from __future__ import annotations

from .test_dash_api import _get_json, _seed, _seed_d1_finding, running_server  # noqa: F401
from .test_dash_render import page_text  # noqa: F401

MOCK_LITERALS = [
    "149.4M", "39.6M", "217M", "107 FINDINGS", "8.7M OF 15M", "2.6M OF 43.8M",
    "603.8M", "429.6M", "120.9M", "$3,952", "$1,860", "1,641",
    "PUT FABLE DOWN UNTIL MONDAY", "[46, 71, 88]", "[95, 120, 88, 250",
    "17:12", "max 5x",
]


def test_pixel_no_mock_literals(page_text):
    """The shipped page carries none of the reference mock's demo literals."""
    for literal in MOCK_LITERALS:
        assert literal not in page_text, literal


FIELD_NAMES = [
    "used_pct", "clock_pct", "behind", "reading_age_hours", "burnt_equiv",
    "left_equiv", "runs_out_at", "resets_at", "burn_equiv_per_hour",
    "burn_usd_per_hour", "pct_of_block_per_hour", "caps_known", "hook_ran",
    "headroom", "short_label", "left_pct", "cap_equiv", "setup_cmd",
    "tokens_saved", "fix_text", "pct_of_cap", "pct_of_week", "dry_count_week",
    "dry_count_month", "led_counts", "week_blocks", "horizon", "evening",
    "current_block", "cache_hit", "floor_usd", "top_leak", "exact_covered",
    "exact_usd", "confidence_label", "corpus_files", "version", "source",
]


def test_pixel_field_contract(page_text, conn, cc_env, running_server):
    """Every mapped payload field name the plan binds is referenced by the
    page source, and the live API payloads actually carry them."""
    for name in FIELD_NAMES:
        assert name in page_text, name

    _seed_d1_finding(conn, cc_env)
    base, _ = running_server

    windows = _get_json(base + "/api/windows")
    for key in ("hero", "headroom", "week", "horizon", "current_block"):
        assert key in windows

    summary = _get_json(base + "/api/summary")
    assert summary["series"]
    row = summary["series"][0]
    for key in ("date", "floor_usd", "exact_usd", "exact_covered"):
        assert key in row

    findings = _get_json(base + "/api/findings")
    assert findings["findings"], "expected the seeded idle-gap session to produce a finding"
    f = findings["findings"][0]
    for key in ("name", "project", "help", "fix_text", "tokens_saved"):
        assert key in f

    meta = _get_json(base + "/api/meta")
    for key in ("version", "corpus_files"):
        assert key in meta


def test_pixel_thresholds_match_mock(page_text):
    """The pxLvl/pxLeftLvl band edges match the mock's own thresholds
    verbatim (source-presence guard; logic itself is inline JS)."""
    assert "p >= 95 ? 4 : p >= 75 ? 3 : p >= 45 ? 2 : p > 0 ? 1 : 0" in page_text
    assert "pl <= 10 ? 4 : pl <= 25 ? 3 : pl <= 55 ? 2 : 1" in page_text


def test_pixel_offmode_sentences(page_text):
    """Caps-unknown / dry-unknown sentences, formerly the ribbon's, now live
    directly in the hero verdict and the week/month panels."""
    assert "Collecting — not enough usage yet" in page_text
    assert "Caps are unknown until Claude Code’s /usage panel has been opened" in page_text
    assert "CAP UNKNOWN — NO DRY COUNT" in page_text
    assert "TOKENS SHOWN, NOT FILL — CAP UNKNOWN" in page_text
    assert "QUOTA OFF · " in page_text
