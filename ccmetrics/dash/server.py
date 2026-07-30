"""127.0.0.1 dashboard on http.server — wave C.

Endpoints /api/summary, /api/projects, /api/project/<key>, /api/live plus the
static glance view. Nothing here runs yet.
"""

from __future__ import annotations

import sys

WAVE = "C"
DEFAULT_PORT = 7433


def serve(port: int = DEFAULT_PORT) -> int:
    print(
        f"dash lands in wave C (would serve http://127.0.0.1:{port}). "
        f"Run `ccmetrics` for the console summary.",
        file=sys.stderr,
    )
    return 2
