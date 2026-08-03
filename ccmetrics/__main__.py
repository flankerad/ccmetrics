"""`python -m ccmetrics` — same entry point as the `ccmetrics` console script.

Exists so the `<python> -m ccmetrics` fallback in plan.resolve_invocation()
is an invocation that actually runs, not just a plausible-looking string.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
