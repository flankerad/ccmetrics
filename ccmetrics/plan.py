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
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from . import constants

STALE_HOURS = constants.value(constants.PLAN["stale_hours"])
MIN_INTERVAL_S = constants.value(constants.PLAN["min_interval_seconds"])

# How long a chained statusline command (--passthrough) may take before
# ccmetrics gives up on it and prints its own line. Deliberately short: this
# runs on every redraw of the user's editor chrome, so a slow child would be
# felt as a frozen status line, not as a late one.
PASSTHROUGH_TIMEOUT_S = 2.0

# short tag for the statusline, long words for the dashboard. Windows Claude
# Code has not documented are shown under their own raw name rather than
# guessed at.
WINDOW_LABELS = {
    "five_hour": ("5h", "last 5 hours"),
    "seven_day": ("wk", "this week"),
    # No per-model entry here on purpose (PLAN-redesign A2): WHICH model gets
    # its own weekly window is an account fact, not a constant -- it arrives on
    # the bar itself, as `scope.model.display_name`.
    "session": ("5h", "current session"),
    "weekly_all": ("wk", "this week"),
}
WINDOW_ORDER = ("seven_day", "five_hour", "session", "weekly_all")

# Key prefix for a weekly window that applies to one model only. The rest of
# the key is that model's own name, lowercased.
SCOPED_PREFIX = "weekly_scoped_"

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


# --- the /usage cache (~/.claude.json) --------------------------------------
#
# A second, optional local source (PLAN-dash-v2 3.1): Claude Code writes its
# own reading of `cachedUsageUtilization` to ~/.claude.json every time the
# /usage panel runs. Unlike the statusline feed above this needs no user
# setup, but it is a snapshot, not a stream -- it only changes when /usage is
# opened again. Read `limits` (`bars` is accepted only as a secondary name,
# PLAN-dash-v2 1.1 correction -- the real file has no `bars` key at all),
# never the named `five_hour`/`seven_day`/`seven_day_opus` placeholders
# inside `utilization`: those are mostly null and, on this account, the
# scoped weekly window is Fable, not Opus -- WHICH model is scoped is an
# account fact that arrives on the entry itself.


def usage_cache_path() -> Path:
    """~/.claude.json, overridable by CCMETRICS_CLAUDE_CONFIG for tests."""
    override = os.environ.get("CCMETRICS_CLAUDE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude.json"


def _cache_reset_iso(v) -> str | None:
    """`limits[].resets_at` (or `bars[].resets_at`) is an ISO-8601 string
    (sub-second), unlike the statusline feed's unix-epoch `resets_at`.
    Garbage in, None out."""
    if not isinstance(v, str) or not v.strip():
        return None
    t = v.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = _dt.datetime.fromisoformat(t)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bar_window(bar: dict, idx: int):
    """One `limits[]` (or `bars[]`) entry -> (sort_order, key, entry) or
    None to skip it.

    Bars with a missing/non-numeric percent are skipped, not defaulted. A
    `kind` we do not recognise is kept under its own raw kind/label rather
    than dropped -- never silently, never guessed at.
    """
    pct = _pct(bar.get("percent"))
    if pct is None:
        return None
    kind = bar.get("kind")
    resets_at = _cache_reset_iso(bar.get("resets_at"))
    severity = bar.get("severity") if isinstance(bar.get("severity"), str) else None
    is_active = bool(bar.get("is_active"))
    model = None

    if kind == "session":
        key, label, short, order = "session", "Current session", "5h", 0
    elif kind == "weekly_all":
        key, label, short, order = "weekly_all", "This week · all models", "wk", 1
    elif kind == "weekly_scoped":
        name = None
        scope = bar.get("scope")
        if isinstance(scope, dict):
            m = scope.get("model")
            if isinstance(m, dict):
                dn = m.get("display_name")
                if isinstance(dn, str) and dn.strip():
                    name = dn.strip()
        cleaned = _clean_name(name) if name else None
        if not cleaned:
            return None  # a scoped bar we cannot name is dropped, not guessed at
        key, label, short, order = SCOPED_PREFIX + cleaned, "This week · " + name, "wk", 2
        model = name
    else:
        if not isinstance(kind, str) or not kind.strip():
            return None
        cleaned = _clean_name(kind) or kind.strip().lower().replace(" ", "_")[:32]
        key, label, short, order = cleaned, kind.replace("_", " "), kind.replace("_", " "), 2

    entry = {
        "used_pct": pct,
        "resets_at": resets_at,
        "label": label,
        "short": short,
        "model": model,
        "is_active": is_active,
        "severity": severity,
    }
    return order, idx, key, entry


def _extract_bars_list(source: list) -> dict:
    entries = []
    for idx, bar in enumerate(source):
        if not isinstance(bar, dict):
            continue
        got = _bar_window(bar, idx)
        if got is not None:
            entries.append(got)
    entries.sort(key=lambda e: (e[0], e[1]))
    out: dict = {}
    for _order, _idx, key, entry in entries:
        out[key] = entry
    return out


def _extract_named_windows(util: dict) -> dict:
    """Fallback when `limits`/`bars` is absent or empty: the named
    `five_hour`/`seven_day` placeholders, nulls ignored."""
    out: dict = {}
    for key in ("five_hour", "seven_day"):
        w = util.get(key)
        if not isinstance(w, dict):
            continue
        pct = _pct(w.get("utilization"))
        if pct is None:
            continue
        short, label = window_labels(key)
        out[key] = {
            "used_pct": pct,
            "resets_at": _cache_reset_iso(w.get("resets_at")),
            "label": label,
            "short": short,
            "model": None,
            "is_active": bool(w.get("is_active")) if "is_active" in w else True,
            "severity": w.get("severity") if isinstance(w.get("severity"), str) else None,
        }
    return out


def read_usage_cache(path=None) -> dict | None:
    """The `/usage` cache Claude Code writes to ~/.claude.json, or None.

    None when the file is missing, unreadable, not JSON, or has no
    `cachedUsageUtilization` block. Never raises.
    """
    p = Path(path) if path else usage_cache_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cu = data.get("cachedUsageUtilization")
    if not isinstance(cu, dict):
        return None
    util = cu.get("utilization")
    if not isinstance(util, dict):
        return None

    fetched_ms = _num(cu.get("fetchedAtMs"))
    fetched_at = None
    if fetched_ms is not None:
        fetched_at = _dt.datetime.fromtimestamp(
            fetched_ms / 1000.0, _dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # PLAN-dash-v2 1.1 correction (2026-08-02): the real file keys this list
    # `limits`, not `bars` -- `bars` is accepted only as a secondary name for
    # future-proofing, in case Claude Code renames it back. Neither present
    # (or an empty list) falls back to the named five_hour/seven_day keys.
    limits = util.get("limits")
    if not isinstance(limits, list):
        limits = util.get("bars")
    if isinstance(limits, list) and limits:
        windows = _extract_bars_list(limits)
    else:
        windows = _extract_named_windows(util)

    return {"fetched_at": fetched_at, "windows": windows}


# --- the plan tier ------------------------------------------------------
#
# `oauthAccount.organizationRateLimitTier` (e.g. `default_claude_max_5x`),
# read never guessed. `organizationType` rides along for context; it is not
# what decides the label.

_KNOWN_TIERS = {
    "default_claude_max_5x": ("Max (5×)", "claude_max"),
    "default_claude_max_20x": ("Max (20×)", "claude_max"),
    "default_claude_pro": ("Pro", "claude_pro"),
    "default_claude_free": ("Free", "claude_free"),
}
_TIER_MAX_PATTERN = re.compile(r"^.*_max_(\d+)x$")


def plan_tier(path=None) -> dict | None:
    """{"key": ..., "label": ..., "type": ...} or None when unrecognised."""
    p = Path(path) if path else usage_cache_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    oauth = data.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    key = oauth.get("organizationRateLimitTier")
    if not isinstance(key, str) or not key.strip():
        return None
    key = key.strip()

    if key in _KNOWN_TIERS:
        label, tier_type = _KNOWN_TIERS[key]
    else:
        m = _TIER_MAX_PATTERN.match(key)
        if not m:
            return None  # unrecognised: no guess, no partial dict
        label, tier_type = "Max (" + m.group(1) + "×)", "claude_max"

    return {"key": key, "label": label, "type": tier_type}


_CREDIT_FIELDS = (
    "is_enabled", "monthly_limit", "used_credits", "utilization",
    "currency", "decimal_places", "disabled_reason",
)


def usage_credits(path=None) -> dict | None:
    """`cachedUsageUtilization.utilization.extra_usage`, whitelisted.

    PLAN-dash-v2 5 correction: this, not the parallel `spend` block, is the
    source -- `extra_usage` is the simpler verified shape. None when the
    file, the `cachedUsageUtilization` block or `extra_usage` itself is
    absent. Never raises.
    """
    p = Path(path) if path else usage_cache_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cu = data.get("cachedUsageUtilization")
    if not isinstance(cu, dict):
        return None
    util = cu.get("utilization")
    if not isinstance(util, dict):
        return None
    extra = util.get("extra_usage")
    if not isinstance(extra, dict):
        return None
    return {k: extra.get(k) for k in _CREDIT_FIELDS}


def record_usage_cache(conn, cache=None, path=None) -> int:
    """Store one `plan_snapshots` row per window in `cache`, returns rows written.

    Written only when `fetched_at` is newer than the newest stored ts for that
    window -- an unchanged cache on a repeat ingest writes zero rows. Never
    raises: called from the ingest path, and a failure to read the cache must
    never fail an ingest.
    """
    from . import store

    if cache is None:
        cache = read_usage_cache(path)
    if not cache:
        return 0
    fetched_at = cache.get("fetched_at")
    windows = cache.get("windows") or {}
    if not fetched_at or not windows:
        return 0

    candidates = {}
    for key, w in windows.items():
        pct = w.get("used_pct")
        if pct is None:
            continue
        candidates[key] = {"used_pct": pct, "resets_at": w.get("resets_at")}
    if not candidates:
        return 0

    fresh = {}
    for key, w in candidates.items():
        row = conn.execute(
            "SELECT MAX(ts) t FROM plan_snapshots WHERE window_key=?", (key,)
        ).fetchone()
        newest = row["t"] if row else None
        write_it = (newest is None) or (newest < fetched_at)
        if write_it:
            fresh[key] = w
    if not fresh:
        return 0
    return store.insert_plan_snapshot(conn, fetched_at, fresh, session_id=None)


def _git_branch(cwd: str) -> str | None:
    """Current branch, read straight off .git/HEAD — no subprocess.

    This runs on every redraw, so it walks up to the repo root and reads one
    small file. A detached HEAD gives a short commit id; anything unreadable
    gives None and the segment simply does not appear.
    """
    try:
        here = Path(cwd).resolve()
    except OSError:
        return None
    for d in (here, *here.parents):
        git = d / ".git"
        try:
            if git.is_file():  # worktree: ".git" is a pointer file
                line = git.read_text(encoding="utf-8", errors="replace").strip()
                if not line.startswith("gitdir:"):
                    return None
                git = Path(line.split(":", 1)[1].strip())
            head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if head.startswith("ref:"):
            return head.split("/", 2)[-1][:60] or None
        return head[:7] or None
    return None


def extract(payload: dict) -> dict:
    """Whitelist the handful of fields we use. Nothing else is ever read.

    The raw object is not stored anywhere: this returns a small dict of
    numbers, model names and timestamps, and that is what reaches the store.
    """
    out: dict = {"session_id": None, "model": None, "cost_usd": None,
                 "context_pct": None, "windows": {}, "project": None,
                 "branch": None, "style": None, "ctx_used": None, "ctx_max": None}
    if not isinstance(payload, dict):
        return out

    ws = payload.get("workspace")
    if isinstance(ws, dict):
        for key in ("project_dir", "current_dir"):
            v = ws.get(key)
            if isinstance(v, str) and v.strip():
                out["project"] = Path(v.strip()).name[:40]
                out["branch"] = _git_branch(v.strip())
                break

    style = payload.get("output_style")
    if isinstance(style, dict):
        v = style.get("name")
        if isinstance(v, str) and v.strip():
            out["style"] = v.strip()[:20]

    sid = payload.get("session_id")
    if isinstance(sid, str) and 0 < len(sid) <= 128:
        out["session_id"] = sid

    model = payload.get("model")
    model_id = model.get("id") if isinstance(model, dict) else None
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
        used = _num(ctx.get("used_tokens"))
        total = (
            _num(ctx.get("max_tokens"))
            or constants.context_window(model_id)
            or constants.context_window(out["model"])
        )
        if used is None and total and out["context_pct"] is not None:
            used = total * out["context_pct"] / 100.0
        out["ctx_used"] = used
        out["ctx_max"] = float(total) if total else None

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


# Colours for the printed line. Claude Code renders ANSI escapes in the status
# line, so these are always emitted unless NO_COLOR is set.
_ORANGE = "\033[38;5;209m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_OFF = "\033[0m"

# The dashboard's four heat stops (--l1..--l4 in dash/static/index.html), as
# hue/saturation/lightness. The status line blends BETWEEN them rather than
# stepping, so 40% used sits visibly between green and yellow instead of
# snapping to one of four colours.
_HEAT_STOPS = (
    (0.0, (136.0, 0.20, 0.39)),   # #4f7a58 green
    (50.0, (63.0, 0.37, 0.46)),   # #9aa04a yellow-green
    (80.0, (30.0, 0.55, 0.51)),   # #c8873c orange
    (100.0, (7.0, 0.52, 0.48)),   # #bd4a3a red
)


def _hsl_rgb(h: float, s: float, ll: float) -> tuple[int, int, int]:
    c = (1 - abs(2 * ll - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = ll - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return tuple(round((v + m) * 255) for v in (r, g, b))


def _heat(pct: float) -> str:
    """Truecolor escape for a used-percentage, green -> orange -> red."""
    pct = max(0.0, min(100.0, float(pct)))
    lo = _HEAT_STOPS[0]
    for stop in _HEAT_STOPS:
        if stop[0] >= pct:
            hi = stop
            break
        lo = stop
    else:
        hi = _HEAT_STOPS[-1]
    span = hi[0] - lo[0]
    t = 0.0 if span <= 0 else (pct - lo[0]) / span
    h, s, ll = (a + (b - a) * t for a, b in zip(lo[1], hi[1]))
    r, g, b = _hsl_rgb(h, s, ll)
    return f"\033[38;2;{r};{g};{b}m"


def _paint(text: str, code: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{code}{text}{_OFF}"


def _fmt_tokens(n: float) -> str:
    """94400 -> '94.4k'; 200000 -> '200k'; 1000000 -> '1M'."""
    if n < 1000:
        return f"{n:.0f}"
    if n >= 1_000_000:
        m = n / 1_000_000.0
        return f"{m:.0f}M" if abs(m - round(m)) < 0.05 else f"{m:.1f}M"
    k = n / 1000.0
    return f"{k:.0f}k" if abs(k - round(k)) < 0.05 else f"{k:.1f}k"


_CTX_BAR_CELLS = 8

# Eighth-width left blocks, thinnest to thickest, for the one partial cell.
# A full cell renders "█" (the eighth ramp doesn't need its own full glyph).
_EIGHTHS = "▏▎▍▌▋▊▉"


def _ctx_bar(pct: float, cells: int = _CTX_BAR_CELLS) -> str:
    """8-cell bar for context usage, sub-divided into eighths so it moves
    about every 1.5% instead of every 12.5%: the one leading cell that isn't
    fully filled renders as a partial Unicode block (▏▎▍▌▋▊▉) sized to the
    remainder, cells behind it are full "█", cells ahead stay dim "░". Any
    nonzero percentage lights at least the thinnest sliver; the bar is always
    exactly `cells` characters wide, at 0% and at 100% alike. Green at the
    left end and red at the right end no matter how full the bar is -- cell i
    (partial or full) is heat-coloured by its own position, (i + 1) / cells,
    not by the overall percentage.
    """
    pct = float(pct)
    if pct != pct:  # NaN never satisfies < or >, so clamp it explicitly.
        pct = 0.0
    pct = max(0.0, min(100.0, pct))
    total_eighths = cells * 8
    filled_eighths = round(pct / 100.0 * total_eighths)
    if pct > 0.0 and filled_eighths == 0:
        filled_eighths = 1
    filled_eighths = min(filled_eighths, total_eighths)
    full_cells, partial = divmod(filled_eighths, 8)
    out = []
    for i in range(cells):
        colour = _heat((i + 1) / cells * 100.0)
        if i < full_cells:
            out.append(_paint("█", colour))
        elif i == full_cells and partial:
            out.append(_paint(_EIGHTHS[partial - 1], colour))
        else:
            out.append(_paint("░", _DIM))
    return "".join(out)


def render_line(data: dict) -> str:
    """The one-line statusline. Only facts that arrived on stdin appear.

    Shape (statusline.sh 'Clean Claude'):
        project > branch | * Model :: style 94.4k/200k (47%) | $21.25 | 5h 68% left | [bar] week 82% left
    Every segment is optional; missing ones close up without leaving separators.
    """
    head = []
    if data.get("project"):
        head.append(_paint(data["project"], _BOLD))
    if data.get("branch"):
        arrow = _paint(">", _DIM)
        head.append(f"{arrow} {_paint(data['branch'], _DIM)}")

    mid = []
    if data.get("model"):
        mid.append(_paint(f"✳ {data['model']}", _ORANGE))
    tail = []
    if data.get("style"):
        tail.append(_paint(data["style"], _ORANGE))
    used, total = data.get("ctx_used"), data.get("ctx_max")
    ctx = data.get("context_pct")
    if used is not None and total:
        pct = f" ({ctx:.0f}%)" if ctx is not None else ""
        text = f"{_fmt_tokens(used)}/{_fmt_tokens(total)}{pct}"
        tail.append(_paint(text, _heat(ctx)) if ctx is not None else text)
    elif ctx is not None:
        tail.append(_paint(f"{ctx:.0f}%", _heat(ctx)))
    if tail:
        joined = " ".join(tail)
        mid.append(f"{_paint('::', _DIM)} {joined}" if mid else joined)

    parts = [" ".join(head)] if head else []
    if mid:
        parts.append(" ".join(mid))
    cost = data.get("cost_usd")
    if cost is not None:
        # Left uncoloured on purpose: unlike the heat-graded tokens and window
        # figures around it, cost has no percentage scale to grade against.
        parts.append(f"${cost:.2f}")
    windows = data.get("windows") or {}
    # Status-line-only spelling for the short labels: the console summary and
    # the dashboard still read WINDOW_LABELS directly and keep "wk" -- this
    # map is local to render_line on purpose so it never touches that dict.
    _STATUSLINE_LABEL = {"wk": "week"}
    # The bar moved here from the context segment (session 2026-08-09,
    # follow-up to item 1): it tracks the weekly plan window's used percent,
    # so it now sits beside the segment it actually describes -- "week NN%
    # left" -- with the bar preceding the number it describes. No weekly
    # reading at all (a non-subscriber, or any payload with no window that
    # prints as "week") means no bar anywhere on the line -- context's own
    # text was always plain and stays that way.
    # shortest window first here, unlike the dashboard: the 5h number is the one
    # that bites soonest, so it reads left to right as urgency.
    for name in sorted(windows, key=lambda n: (-_window_sort(n)[0], n)):
        used_pct = windows[name].get("used_pct")
        if used_pct is None:
            continue
        short, _long = window_labels(name)
        short = _STATUSLINE_LABEL.get(short, short)
        left_pct = 100 - used_pct
        # _heat grades by how much is SPENT, not how much is left -- keep
        # feeding it used_pct even though the printed number is now the
        # remainder, or a nearly-exhausted window (5% left) would paint
        # green instead of red.
        # Gated on the LABEL ("week"), not the raw key ("seven_day") -- any
        # window WINDOW_LABELS maps to "wk"/"week" (seven_day, weekly_all)
        # gets the bar, so it can never silently vanish just because a
        # different weekly key won this row. A weekly_scoped_* window falls
        # outside WINDOW_LABELS entirely (its own short label is the raw,
        # de-underscored key, not "week") and so carries no bar -- a
        # pre-existing labelling gap, not something this gate introduces.
        bar = f"{_ctx_bar(used_pct)} " if short == "week" else ""
        parts.append(bar + _paint(f"{short} {left_pct:.0f}% left", _heat(used_pct)))
    if not parts:
        # Nothing arrived on stdin: name where we are rather than name ourselves,
        # so the line still tells the user something true.
        return _paint(Path.cwd().name or "ccmetrics", _BOLD)
    return f" {_paint('|', _DIM)} ".join(parts)


def _passthrough(command: str, stdin_text: str) -> str | None:
    """Run the user's own statusline command on the same stdin, return its line.

    Claude Code allows exactly ONE statusline command. Rather than displace
    whatever the user already had there, ccmetrics takes the slot and hands the
    payload straight on. Everything here is best-effort: any failure returns
    None and the caller falls back to ccmetrics' own line. The timeout is short
    on purpose -- a hung child would freeze the status line on every redraw,
    which is worse than a missing line.
    """
    import subprocess

    if not command or not command.strip():
        return None
    try:
        proc = subprocess.run(
            command,
            shell=True,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=PASSTHROUGH_TIMEOUT_S,
        )
    except Exception:
        return None  # missing binary, timeout, bad shell -- all the same answer
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip("\n")
    return out if out.strip() else None


def run(stdin_text: str, passthrough: str | None = None) -> str:
    """Read one JSON object, store what it says about the plan, return the line.

    Every step is optional: unparseable input still prints something, and a
    store that cannot be opened still prints the model and cost from stdin.

    `passthrough` is the user's own statusline command, if they had one. The
    payload is read once and handed to every one of them: ccmetrics records the
    plan snapshot from it, then prints their output AND its own line, joined,
    so owning the single statusline slot costs the user nothing. Anything going
    wrong with one -- missing, failing, slow, silent -- simply leaves it out,
    because a status line must never go blank or loud.
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
    commands = [passthrough] if isinstance(passthrough, str) else list(passthrough or [])
    borrowed = []
    for cmd in commands:
        try:
            line = _passthrough(cmd, stdin_text)
        except Exception:
            line = None
        if line:
            borrowed.append(line)
    ours = render_line(data)
    if borrowed:
        # Both are shown, borrowed first: the user's own prompt is what they
        # read by habit, ours is the number they consult.
        return f" {_paint('|', _DIM)} ".join([*borrowed, ours])
    return ours


# --- setup ------------------------------------------------------------------


def setup_text() -> str:
    fragment = json.dumps(
        # `refreshInterval: 5` keeps the line refreshing while the session
        # sits idle -- Claude Code's own updates are event-driven, and go
        # quiet exactly when a coordinator is waiting on background
        # subagents, which is when the plan % is most likely to have moved.
        {"statusLine": {"type": "command", "command": "ccmetrics statusline",
                         "refreshInterval": 5}},
        indent=2,
    )
    chained = json.dumps(
        {
            "statusLine": {
                "type": "command",
                "command": "ccmetrics statusline --passthrough 'YOUR EXISTING COMMAND'",
            }
        },
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
            "Already have a status line? Claude Code runs exactly one command, so",
            "`ccmetrics setup --apply` takes the slot: it backs up whatever command",
            "was there to a `.bak-ccmetrics` file next to settings.json, then writes",
            "the command above. `ccmetrics setup --revert` puts the old one back.",
            "",
            "Want yours to keep running too? Wrap it by hand with --passthrough:",
            "ccmetrics reads the payload, records your plan %, then runs your",
            "command on the same payload and prints ITS output:",
            "",
            chained,
            "",
            f"If that command is missing, fails, prints nothing or takes longer than",
            f"{PASSTHROUGH_TIMEOUT_S:.0f}s, ccmetrics prints its own line instead and still exits 0.",
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


# --- setup: --apply / --revert / --check -------------------------------------


def default_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


class SetupError(Exception):
    """Refused to touch settings.json — message is safe to print as-is."""


PROBE_TIMEOUT_S = 2.0


def _probe_supports_passthrough(exe_tokens: list[str]) -> bool | None:
    """Runs `<exe> statusline --help` and checks whether --passthrough is
    listed in it -- the cheapest proof that a resolved build actually
    supports the flag `--apply` writes, rather than merely existing under
    that name.

    True/False once the probe actually completes; None when it couldn't be
    completed at all (missing binary, timeout, crash) -- an unknown, never
    treated as "unsupported". Never touches stdin or the store, and is
    always bounded by a short timeout, so a broken binary can't hang
    `--check` or `--apply`.
    """
    import subprocess

    try:
        proc = subprocess.run(
            [*exe_tokens, "statusline", "--help"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return "--passthrough" in (proc.stdout or "")


def _exe_tokens(command: str) -> list[str] | None:
    """The executable argv prefix of a `ccmetrics statusline` command, e.g.
    ['ccmetrics'] or the `<python> -m ccmetrics` form. None if `command`
    doesn't look like one of ours.
    """
    tokens = _split_command(command)
    if not tokens or "statusline" not in tokens:
        return None
    idx = tokens.index("statusline")
    exe = tokens[:idx]
    return exe or None


def resolve_invocation() -> tuple[str, str | None]:
    """How this ccmetrics should be invoked from another process's shell.

    Returns (invocation, warning). Prefers a bare `ccmetrics` when it
    resolves on PATH to a build that actually matches what's running this
    process — the friendliest thing to leave in someone's settings.json.

    But a bare command is only as good as whatever `ccmetrics` happens to
    resolve to: on a machine with an older `uv tool install` shadowing this
    checkout, it can be a stale build that predates a flag this checkout
    relies on (e.g. --passthrough), so wiring it up silently produces a
    status line that errors on every redraw. When PATH's `ccmetrics` isn't
    provably the exact file running this process, this probes it for
    --passthrough support; if that's missing, unconfirmed, or the file
    plainly differs, it falls back to the absolute path of the
    currently-running executable instead — guaranteed to behave exactly as
    just verified — and returns a warning explaining the substitution.

    Falls back further, to `<python> -m ccmetrics`, if even the running
    executable is not a real file on disk (e.g. it was run as
    `python -m ccmetrics` directly).
    """
    self_exe = Path(sys.argv[0]).resolve()
    fallback = str(self_exe) if self_exe.exists() else f"{shlex.quote(sys.executable)} -m ccmetrics"

    which_path = shutil.which("ccmetrics")
    if not which_path:
        return fallback, None

    which_resolved = Path(which_path).resolve()
    if self_exe.exists() and which_resolved == self_exe:
        return "ccmetrics", None

    support = _probe_supports_passthrough([which_path])
    if support is False:
        reason = "does not support --passthrough — it's an older build than the one running this setup"
    elif support is None:
        reason = "could not be confirmed to match this build (the --help probe failed)"
    else:
        reason = "is a different file than the one currently running this setup"
    return fallback, (
        f"WARNING: the ccmetrics on PATH ({which_path}) {reason}. Writing "
        f"the absolute path of this build ({fallback}) into settings.json "
        "instead of bare `ccmetrics`, so the status line is guaranteed to "
        "work. To fix `ccmetrics` on PATH too, reinstall ccmetrics from "
        "this checkout (e.g. `uv tool install --force .`)."
    )


def _split_command(command: str) -> list[str] | None:
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _is_ours_command(command: str) -> bool:
    """True if `command` is a `ccmetrics statusline` invocation, plain or wrapped."""
    tokens = _split_command(command)
    if not tokens or "statusline" not in tokens:
        return False
    idx = tokens.index("statusline")
    exe_tokens = tokens[:idx]
    if len(exe_tokens) == 1:
        return Path(exe_tokens[0]).name == "ccmetrics"
    if len(exe_tokens) == 3 and exe_tokens[1] == "-m" and exe_tokens[2] == "ccmetrics":
        return True
    return False


def _extract_passthrough(command: str) -> str | None:
    """Pull the wrapped command back out of `--passthrough '<cmd>'`, if present."""
    tokens = _split_command(command) or []
    if "--passthrough" not in tokens:
        return None
    i = tokens.index("--passthrough")
    return tokens[i + 1] if i + 1 < len(tokens) else None


def _command_resolves(command: str) -> bool:
    """Whether the executable named at the front of `command` would actually run."""
    tokens = _split_command(command)
    if not tokens:
        return False
    exe = tokens[0]
    if Path(exe).is_absolute():
        return Path(exe).exists() and os.access(exe, os.X_OK)
    return shutil.which(exe) is not None


def _load_settings(settings_path: Path) -> tuple[dict | None, str | None]:
    """Returns (data, raw). `raw` is None when the file did not exist yet;
    `data` is None when `raw` exists but isn't a parseable JSON object --
    invalid JSON or a non-dict top level. Never raises: `apply_setup`
    overwrites unconditionally after backing up the raw text, and read-only
    callers report the same condition instead of refusing.
    """
    if not settings_path.exists():
        return {}, None
    raw = settings_path.read_text()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return None, raw
    if not isinstance(data, dict):
        return None, raw
    return data, raw


def _backup(settings_path: Path, raw: str) -> Path:
    backup_path = settings_path.with_name(settings_path.name + ".bak-ccmetrics")
    backup_path.write_text(raw)
    return backup_path


def _refresh_interval(sl) -> int | float:
    """5, unless the settings file already sets its own refreshInterval --
    that's the user's call, made deliberately, and setup must not clobber
    it. Event-driven status-line updates go quiet exactly when a
    coordinator is waiting on background subagents (documented Claude Code
    behaviour), which is when the plan % is most likely to have moved since
    the last render -- refreshInterval keeps the line polling regardless.
    """
    existing = sl.get("refreshInterval") if isinstance(sl, dict) else None
    return existing if isinstance(existing, (int, float)) and not isinstance(existing, bool) else 5

def apply_setup(settings_path: Path) -> dict:
    """Wire `ccmetrics statusline` into statusLine.command.

    Returns {"changed": bool, "message": str, ...}. Never refuses: whatever
    is there -- unreadable file, malformed statusLine, someone else's
    command -- is backed up and then overwritten with ours. This is a
    deliberate product decision, not an oversight; the backup plus the
    printed `ccmetrics setup --revert` hint is the safety net.
    """
    data, raw = _load_settings(settings_path)
    invocation, invocation_warning = resolve_invocation()
    plain_command = f"{invocation} statusline"

    if data is None:
        # `raw` exists but isn't a parseable JSON object -- nothing in it
        # can be merged, so back up the raw text whole and start fresh.
        backup_path = _backup(settings_path, raw)
        interval = _refresh_interval(None)
        data = {"statusLine": {"type": "command", "command": plain_command,
                                "refreshInterval": interval}}
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        lines = []
        if invocation_warning:
            lines.append(invocation_warning)
        lines += [
            f"{settings_path} was not valid JSON — backed it up and wrote a "
            "fresh file holding only our statusLine.",
            f"written to {settings_path}",
            f"backup of the unreadable file: {backup_path}",
            "to restore it: ccmetrics setup --revert",
        ]
        return {
            "changed": True,
            "old": "(unreadable file)",
            "new": plain_command,
            "backup": backup_path,
            "invocation_warning": invocation_warning,
            "message": "\n".join(lines),
        }

    sl = data.get("statusLine")
    interval = _refresh_interval(sl)

    def _replace_malformed(old_desc, note: str) -> dict:
        # Shared tail for both "statusLine isn't a command dict" and
        # "statusLine.command isn't a usable string" -- same fix either
        # way: back up the raw file, keep every other top-level key, and
        # drop in ours.
        backup_path = _backup(settings_path, raw)
        data["statusLine"] = {"type": "command", "command": plain_command,
                               "refreshInterval": interval}
        settings_path.write_text(json.dumps(data, indent=2) + "\n")
        lines = []
        if invocation_warning:
            lines.append(invocation_warning)
        lines += [
            f"{note} ({old_desc!r}) replaced with ours.",
            f"statusLine.command: {old_desc!r} -> {plain_command!r}",
            f"written to {settings_path}",
            f"backup: {backup_path}",
            "to restore it: ccmetrics setup --revert",
        ]
        return {
            "changed": True,
            "old": old_desc,
            "new": plain_command,
            "backup": backup_path,
            "invocation_warning": invocation_warning,
            "message": "\n".join(lines),
        }

    if sl is None:
        old_desc = "(none)"
        new_command = plain_command
    else:
        if not isinstance(sl, dict) or sl.get("type") != "command":
            return _replace_malformed(sl, "statusLine we don't understand")
        current = sl.get("command")
        if not isinstance(current, str) or not current.strip():
            return _replace_malformed(current, "statusLine.command we don't understand")
        if _is_ours_command(current):
            wrapped = _extract_passthrough(current)
            if wrapped is None:
                if isinstance(sl, dict) and sl.get("refreshInterval") == interval:
                    return {
                        "changed": False,
                        "message": f"already wired: {current!r} — nothing to change.",
                    }
                # Wired already, but this settings file predates
                # refreshInterval (or holds a different one to respect) --
                # the command itself is untouched, only the interval moves.
                data["statusLine"] = {"type": "command", "command": current,
                                       "refreshInterval": interval}
                settings_path.write_text(json.dumps(data, indent=2) + "\n")
                return {
                    "changed": True,
                    "old": current,
                    "new": current,
                    "message": f"statusLine.refreshInterval: -> {interval} (command unchanged: {current!r})",
                }
            # An older ccmetrics build wrapped a command instead of replacing
            # it. Unwrap it now so only ccmetrics renders the status line.
            backup_path = settings_path.with_name(settings_path.name + ".bak-ccmetrics")
            # The backup slot records the command we displaced, never one of
            # ours. If it already holds something usable, that's the
            # displaced command from whenever the wrap was first applied —
            # leave it alone, or we'd overwrite it with a now-wired (ours)
            # settings file and lose the command for good. Only write a
            # fresh backup, holding the unwrapped command, when there's
            # nothing usable there yet. (That means a PRE-EXISTING backup's
            # own refreshInterval — present or absent — wins on revert by
            # the same rule: it describes whatever settings state was
            # current when the wrap first happened, not this apply. Only a
            # backup synthesized HERE, just below, carries this call's
            # current refreshInterval forward.)
            if _read_backed_up_command(settings_path) is None:
                backup_data = dict(data)
                backup_data["statusLine"] = {"type": "command", "command": wrapped}
                # Whatever refreshInterval this (already-ours) block
                # currently carries travels into the synthesized backup too
                # -- otherwise a later revert has nothing to restore it
                # from and silently drops it (GAP 1, session 2026-08-09).
                existing_interval = sl.get("refreshInterval") if isinstance(sl, dict) else None
                if isinstance(existing_interval, (int, float)) and not isinstance(existing_interval, bool):
                    backup_data["statusLine"]["refreshInterval"] = existing_interval
                backup_path.write_text(json.dumps(backup_data, indent=2) + "\n")
            data["statusLine"] = {"type": "command", "command": plain_command,
                                   "refreshInterval": interval}
            settings_path.write_text(json.dumps(data, indent=2) + "\n")
            lines = []
            if invocation_warning:
                lines.append(invocation_warning)
            lines += [
                f"statusLine.command: {current!r} -> {plain_command!r}",
                f"chained command moved to the backup: {wrapped!r}",
                f"written to {settings_path}",
                f"backup: {backup_path}",
            ]
            return {
                "changed": True,
                "old": current,
                "new": plain_command,
                "backup": backup_path,
                "invocation_warning": invocation_warning,
                "message": "\n".join(lines),
            }
        old_desc = current
        new_command = plain_command

    backup_path = _backup(settings_path, raw) if raw is not None else None
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    data["statusLine"] = {"type": "command", "command": new_command, "refreshInterval": interval}
    settings_path.write_text(json.dumps(data, indent=2) + "\n")

    lines = []
    if invocation_warning:
        lines.append(invocation_warning)
    lines += [
        f"statusLine.command: {old_desc!r} -> {new_command!r}",
        f"written to {settings_path}",
        f"backup: {backup_path}" if backup_path else "backup: none (file was created fresh)",
    ]
    return {
        "changed": True,
        "old": old_desc,
        "new": new_command,
        "backup": backup_path,
        "invocation_warning": invocation_warning,
        "message": "\n".join(lines),
    }


def _read_backed_up_command(settings_path: Path) -> str | None:
    """The statusLine.command the `.bak-ccmetrics` sibling held, if any.

    Anything short of a clean read counts as none: file missing, unreadable,
    not JSON, not the shape we write, or a command that's ours already.
    Never raises — callers fall back to just removing statusLine.
    """
    backup_path = settings_path.with_name(settings_path.name + ".bak-ccmetrics")
    try:
        data = json.loads(backup_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sl = data.get("statusLine")
    if not isinstance(sl, dict) or sl.get("type") != "command":
        return None
    command = sl.get("command")
    if not isinstance(command, str) or not command.strip() or _is_ours_command(command):
        return None
    return command


def _read_backed_up_statusline(settings_path: Path) -> tuple[bool, object]:
    """The raw `statusLine` value the `.bak-ccmetrics` sibling held, exactly
    as JSON left it -- for the case `_read_backed_up_command` isn't built
    for: `apply_setup` replaced a statusLine that wasn't a usable command
    at all (wrong type, missing/empty command, non-dict). That value, not
    just its command, is what revert must put back. Returns (True, value)
    when the backup has a statusLine key that isn't already ours; (False,
    None) when the backup is missing, unreadable, has no statusLine key, or
    already holds ours.
    """
    backup_path = settings_path.with_name(settings_path.name + ".bak-ccmetrics")
    try:
        data = json.loads(backup_path.read_text())
    except (OSError, ValueError):
        return False, None
    if not isinstance(data, dict) or "statusLine" not in data:
        return False, None
    sl = data["statusLine"]
    if isinstance(sl, dict) and sl.get("type") == "command":
        command = sl.get("command")
        if isinstance(command, str) and command.strip() and _is_ours_command(command):
            return False, None
    return True, sl


def _read_backed_up_refresh_interval(settings_path: Path):
    """The refreshInterval the `.bak-ccmetrics` sibling's statusLine block
    held before ccmetrics ever touched it, if any. `apply_setup` promises to
    respect a user-set refreshInterval (see `_refresh_interval`); revert must
    honour that same promise on the way back out, rather than always
    stripping whatever value is currently there -- which would silently
    delete a user's own setting the moment it happens to equal ours (5) or
    anything else. None means "the user never set one" -- the field should
    not exist on the restored command either.
    """
    backup_path = settings_path.with_name(settings_path.name + ".bak-ccmetrics")
    try:
        data = json.loads(backup_path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    sl = data.get("statusLine")
    if not isinstance(sl, dict):
        return None
    interval = sl.get("refreshInterval")
    return interval if isinstance(interval, (int, float)) and not isinstance(interval, bool) else None


def revert_setup(settings_path: Path) -> dict:
    """Undo apply_setup.

    A `--passthrough`-carrying command restores as-is — settings written by
    an older ccmetrics build revert without needing the backup. Otherwise we
    read the `.bak-ccmetrics` sibling for the command we displaced; a
    missing, unreadable, or malformed backup never raises, it just means we
    fall back to removing statusLine.
    """
    data, raw = _load_settings(settings_path)
    if raw is None:
        return {"changed": False, "message": f"{settings_path} does not exist — nothing to revert."}
    if data is None:
        return {"changed": False, "message": f"{settings_path} is not valid JSON — nothing to revert."}
    sl = data.get("statusLine")
    if not isinstance(sl, dict) or sl.get("type") != "command":
        return {"changed": False, "message": "statusLine is not wired to ccmetrics — nothing to revert."}
    current = sl.get("command")
    if not isinstance(current, str) or not _is_ours_command(current):
        return {"changed": False, "message": "statusLine is not wired to ccmetrics — nothing to revert."}

    passthrough = _extract_passthrough(current)
    # Read the backup before _backup() below overwrites it with the
    # (still-wired) settings we're about to revert. The pre-apply
    # refreshInterval (if the user had one) lives in that same backup, and
    # must come back with the command rather than being stripped -- popping
    # it unconditionally would silently delete a value _refresh_interval
    # promised on the way in to leave alone (see `_read_backed_up_refresh_interval`).
    displaced = None if passthrough else _read_backed_up_command(settings_path)
    old_interval = _read_backed_up_refresh_interval(settings_path)
    # Only relevant when neither of the above found anything usable --
    # `apply_setup` may have replaced a statusLine that wasn't a command at
    # all (case handled by neither helper above), and that raw value is
    # what belongs back, not a stripped statusLine.
    malformed_present, malformed_value = (
        (False, None) if passthrough or displaced is not None
        else _read_backed_up_statusline(settings_path)
    )

    backup_path = _backup(settings_path, raw)
    if passthrough:
        data["statusLine"]["command"] = passthrough
        if old_interval is None:
            data["statusLine"].pop("refreshInterval", None)
        else:
            data["statusLine"]["refreshInterval"] = old_interval
        new_desc = passthrough
    elif displaced is not None:
        data["statusLine"]["command"] = displaced
        if old_interval is None:
            data["statusLine"].pop("refreshInterval", None)
        else:
            data["statusLine"]["refreshInterval"] = old_interval
        new_desc = displaced
    elif malformed_present:
        data["statusLine"] = malformed_value
        new_desc = malformed_value
    else:
        del data["statusLine"]
        new_desc = "(removed)"
    settings_path.write_text(json.dumps(data, indent=2) + "\n")

    lines = [
        f"statusLine.command: {current!r} -> {new_desc!r}",
        f"written to {settings_path}",
        f"backup: {backup_path}",
    ]
    return {
        "changed": True,
        "old": current,
        "new": new_desc,
        "backup": backup_path,
        "message": "\n".join(lines),
    }


def check_setup(settings_path: Path, conn=None) -> str:
    """Read-only: is the status line wired to us, and when did the plan feed
    last hear from Claude Code. Never writes anything.
    """
    lines: list[str] = []
    data, raw = _load_settings(settings_path)

    if raw is None:
        lines.append(f"{settings_path}: no settings file — status line not wired.")
    elif data is None:
        lines.append(f"{settings_path} is not valid JSON — status line can't be checked.")
    else:
        sl = data.get("statusLine")
        if not isinstance(sl, dict) or sl.get("type") != "command":
            lines.append("status line: not wired to ccmetrics.")
        else:
            cmd = sl.get("command")
            if not isinstance(cmd, str) or not _is_ours_command(cmd):
                lines.append(f"status line: wired to something else ({cmd!r}).")
            else:
                lines.append(f"status line: wired to ccmetrics ({cmd!r}).")
                if not _command_resolves(cmd):
                    lines.append(
                        "WARNING: that command does not resolve on this machine — "
                        "the status line will print nothing. Re-run `ccmetrics setup --apply`."
                    )
                elif _extract_passthrough(cmd) is not None:
                    exe_tokens = _exe_tokens(cmd)
                    if exe_tokens and "/" not in exe_tokens[0]:
                        resolved = shutil.which(exe_tokens[0])
                        if resolved:
                            exe_tokens = [resolved, *exe_tokens[1:]]
                    support = _probe_supports_passthrough(exe_tokens) if exe_tokens else None
                    if support is False:
                        lines.append(
                            "WARNING: the ccmetrics that resolves on PATH does not "
                            "support --passthrough — it's an older build than this "
                            "command needs, so the status line will print an argparse "
                            "error on every redraw instead of recording anything. "
                            "Reinstall ccmetrics from this checkout so the copy on "
                            "PATH matches (e.g. `uv tool install --force .`), then "
                            "run `ccmetrics setup --apply` again."
                        )
                    elif support is None:
                        lines.append(
                            "note: could not confirm the resolved ccmetrics supports "
                            "--passthrough (the --help probe failed) — if the status "
                            "line goes blank, reinstall ccmetrics from this checkout."
                        )

    if conn is not None:
        from . import store

        windows = store.latest_plan_windows(conn)
        ts_values = [w["ts"] for w in windows.values() if w.get("ts")]
        if ts_values:
            lines.append(f"most recent plan reading stored: {max(ts_values)}")
        else:
            lines.append(
                "no plan reading stored yet — it appears after the first reply of a session."
            )

    return "\n".join(lines)
