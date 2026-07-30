"""Leak detectors — wave B.

Twelve detectors per PRD R3, every threshold sourced from constants.py
(DETECTOR_THRESHOLDS), every finding carrying a paste-ready fix per R4b and a
ranking of tokens_saved / effort_tier.

Wave A ships the store, the cost floor and the console; nothing here runs yet.
"""

from __future__ import annotations

WAVE = "B"
NOT_YET = "detectors land in wave B"


def run_all(conn, project: str | None = None) -> list[dict]:
    """No detectors implemented in wave A. Returns an empty finding list."""
    return []
