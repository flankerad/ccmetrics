"""Local dashboard package — wave C."""

from __future__ import annotations


def serve(port: int = 7433) -> int:
    from .server import serve as _serve

    return _serve(port=port)
