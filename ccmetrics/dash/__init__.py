"""Local dashboard package — 127.0.0.1 only."""

from __future__ import annotations


def serve(port: int = 7433, open_browser: bool = True) -> int:
    from .server import serve as _serve

    return _serve(port=port, open_browser=open_browser)
