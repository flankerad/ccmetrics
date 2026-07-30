"""Live current-session tiles (PRD R7) — wave C.

Tails the newest-mtime JSONL under the active project from the R1 watermark and
derives burn rate, context %, cache-hit ratio and session floor $.

Wave A ships the store and the cost floor those tiles will read; nothing here
runs yet.
"""

from __future__ import annotations

WAVE = "C"
NOT_YET = "live tiles land in wave C"


def tiles(conn, project: str | None = None) -> dict:
    return {"status": NOT_YET}
