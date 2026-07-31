"""Optional plan-limit feed — `ccmetrics statusline`.

Claude Code can run one user-configured command every time it redraws its
status line, and it hands that command a JSON object on stdin. For Claude.ai
subscribers that object carries `rate_limits`: the same percentages `/usage`
shows, straight from Anthropic. That is the ONLY honest local source of "how
much of my plan is gone" — no local file holds it, and ccmetrics never
estimates it.

Contract (https://code.claude.com/docs/en/statusline, checked 2026-07-31):

    {
      "session_id": "...",
      "model": {"id": "claude-opus-5", "display_name": "Opus"},
      "cost": {"total_cost_usd": 0.01234, ...},
      "context_window": {"used_percentage": 8, ...},
      "rate_limits": {
        "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
        "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}
      }
    }

`rate_limits` appears only for Pro/Max subscribers, and only after the first
API response of a session; each window may be absent on its own. So every
field here is optional by default: absent means store nothing, print less, and
never crash — a broken statusline command would break the user's editor view.
"""

from __future__ import annotations

import datetime as _dt
import json

from . import constants

STALE_HOURS = constants.value(constants.PLAN["stale_hours"])
MIN_INTERVAL_S = constants.value(constants.PLAN["min_interval_seconds"])

# short tag for the statusline, long words for the dashboard. Windows Claude
# Code has not documented are shown under their own raw name rather than
# guessed at.
WINDOW_LABELS = {
    "five_hour": ("5h", "last 5 hours"),
    "seven_day": ("wk", "this week"),
    "seven_day_opus": ("opus wk", "this week · Opus"),
}
WINDOW_ORDER = ("seven_day", "five_hour")

_NAME_OK = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def window_labels(name: str) -> tuple[str, str]:
    return WINDOW_LABELS.get(name, (name.replace("_", " "), name.replace("_", " ")))


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v):
    """A finite number, or None. Booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _pct(v):
    f = _num(v)
    # documented as 0-100. Anything outside that is a contract we do not
    # understand, and a number we will not reinterpret.
    if f is None or f < 0 or f > 100:
        return None
    return round(f, 1)


def _reset_iso(v):
    """`resets_at` is unix epoch seconds. Out-of-era values are dropped."""
    f = _num(v)
    if f is None or not (1_500_000_000 < f < 4_100_000_000):
        return None
    return _dt.datetime.fromtimestamp(f, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_name(name) -> str | None:
    if not isinstance(name, str):
        return None
    n = name.strip().lower()
    if not n or len(n) > 32 or not set(n) <= _NAME_OK:
        return None
    return n


def extract(payload: dict) -> dict:
    """Whitelist the handful of fields we use. Nothing else is ever read.

    The raw object is not stored anywhere: this returns a small dict of
    numbers, model names and timestamps, and that is what reaches the store.
    """
    out: dict = {"session_id": None, "model": None, "cost_usd": None,
                 "context_pct": None, "windows": {}}
    if not isinstance(payload, dict):
        return out

    sid = payload.get("session_id")
    if isinstance(sid, str) and 0 < len(sid) <= 128:
        out["session_id"] = sid

    model = payload.get("model")
    if isinstance(model, dict):
        for key in ("display_name", "id"):
            v = model.get(key)
            if isinstance(v, str) and v.strip():
                out["model"] = v.strip()[:40]
                break

    cost = payload.get("cost")
    if isinstance(cost, dict):
        out["cost_usd"] = _num(cost.get("total_cost_usd"))

    ctx = payload.get("context_window")
    if isinstance(ctx, dict):
        out["context_pct"] = _pct(ctx.get("used_percentage"))

    limits = payload.get("rate_limits")
    if isinstance(limits, dict):
        for raw_name, w in limits.items():
            name = _clean_name(raw_name)
            if not name or not isinstance(w, dict):
                continue
            used = _pct(w.get("used_percentage"))
            if used is None:  # a window with no percentage tells us nothing
                continue
            out["windows"][name] = {"used_pct": used, "resets_at": _reset_iso(w.get("resets_at"))}
    return out


def record(conn, data: dict, now_iso: str | None = None) -> int:
    """Store the snapshot, then trim old ones. Returns rows written.

    The statusline command runs on every redraw, so identical snapshots would
    pile up; anything closer than PLAN.min_interval_seconds to the last one is
    dropped on the floor.
    """
    from . import store

    windows = data.get("windows") or {}
    if not windows:
        return 0
    now_iso = now_iso or _now_iso()
    last = store.last_plan_ts(conn)
    gap = _age_seconds(last, now_iso) if last else None
    if gap is not None and gap < MIN_INTERVAL_S:
        return 0
    n = store.insert_plan_snapshot(conn, now_iso, windows, data.get("session_id"))
    store.prune_plan_snapshots(conn, now_iso)
    return n


def _parse_iso(ts: str):
    try:
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _age_seconds(ts: str, now_iso: str | None = None):
    then = _parse_iso(ts)
    now = _parse_iso(now_iso or _now_iso())
    if then is None or now is None:
        return None
    return max(0.0, (now - then).total_seconds())


def snapshot_view(windows: dict, now_iso: str | None = None) -> list[dict]:
    """Latest-per-window rows, oldest-first fields filled in: age and staleness.

    Staleness is stated, never used to hide a number silently on the page — a
    6-hour-old figure for a 5-hour window is history, and it says so.
    """
    now_iso = now_iso or _now_iso()
    out = []
    for name in sorted(windows, key=_window_sort):
        w = windows[name]
        age = _age_seconds(w.get("ts"), now_iso)
        short, long = window_labels(name)
        resets_in = None
        reset_dt = _parse_iso(w.get("resets_at")) if w.get("resets_at") else None
        now_dt = _parse_iso(now_iso)
        if reset_dt is not None and now_dt is not None:
            resets_in = (reset_dt - now_dt).total_seconds()
        out.append(
            {
                "window": name,
                "short_label": short,
                "label": long,
                "used_pct": w.get("used_pct"),
                "resets_at": w.get("resets_at"),
                "resets_in_seconds": resets_in,
                "ts": w.get("ts"),
                "age_seconds": age,
                "stale": age is None or age > STALE_HOURS * 3600,
            }
        )
    return out


def _window_sort(name: str) -> tuple:
    return (WINDOW_ORDER.index(name) if name in WINDOW_ORDER else len(WINDOW_ORDER), name)


def fmt_reset(iso: str | None) -> str:
    """'Sun 14:00' in the user's own timezone, or '' when unknown."""
    d = _parse_iso(iso) if iso else None
    if d is None:
        return ""
    return d.astimezone().strftime("%a %H:%M")


# --- the printed line -------------------------------------------------------


def render_line(data: dict) -> str:
    """The one-line statusline. Only facts that arrived on stdin appear."""
    from . import costs

    parts = []
    if data.get("model"):
        parts.append(data["model"])
    cost = data.get("cost_usd")
    if cost is not None:
        parts.append(costs.fmt_usd(cost))
    windows = data.get("windows") or {}
    for name in sorted(windows, key=_window_sort):
        pct = windows[name].get("used_pct")
        if pct is None:
            continue
        short, _long = window_labels(name)
        parts.append(f"{short} {pct:.0f}%")
    ctx = data.get("context_pct")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    return " · ".join(parts) if parts else "ccmetrics"


def run(stdin_text: str) -> str:
    """Read one JSON object, store what it says about the plan, return the line.

    Every step is optional: unparseable input still prints something, and a
    store that cannot be opened still prints the model and cost from stdin.
    """
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except ValueError:
        payload = {}
    data = extract(payload)
    if data.get("windows"):
        try:
            from . import store

            conn = store.connect()
            try:
                record(conn, data)
            finally:
                conn.close()
        except Exception:
            pass  # a statusline must never fail loudly: it is the user's editor chrome
    return render_line(data)


# --- setup ------------------------------------------------------------------


def setup_text() -> str:
    fragment = json.dumps(
        {"statusLine": {"type": "command", "command": "ccmetrics statusline"}},
        indent=2,
    )
    return "\n".join(
        [
            "ccmetrics statusline — your real plan usage (optional)",
            "",
            "Claude Code runs one command of your choosing to draw its status line,",
            "and hands that command its own session JSON. On a Pro/Max plan that JSON",
            "carries your rate-limit percentages — the same ones /usage shows. This is",
            "the only place those numbers exist on your machine: no local file holds",
            "them, so without this hook ccmetrics will never show a plan %, and it",
            "will never estimate one.",
            "",
            "Turn it on by adding this to ~/.claude/settings.json:",
            "",
            fragment,
            "",
            "You then get a status line like:",
            "",
            "    Opus · $0.31 · wk 62% · 5h 31% · ctx 24%",
            "",
            "and a PLAN card on `ccmetrics dash`.",
            "",
            "notes: local only, nothing is sent anywhere · stored per update: the",
            "        percentage, the reset time and the session id — no prompt text,",
            f"        no file contents · kept "
            f"{constants.value(constants.PLAN['retention_days'])} days · figures older "
            f"than {STALE_HOURS}h are labelled",
            "        stale (a 5-hour window has rolled over by then).",
            "        Percentages only appear after the first reply in a session, and",
            "        only on a Claude.ai subscription — on API billing Claude Code",
            "        sends no rate_limits at all.",
            f"        contract: {constants.STATUSLINE_DOC}",
        ]
    )
