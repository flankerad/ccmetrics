"""Always-on-top desktop widget: the dash hero's fuse, nothing else.

The page's hero is two parts -- the fuse (one bar, the verdict sentence, the
burn rate, when the week runs out) and the headroom rows (one meter per limit).
Only the fuse travels here: it is the part worth keeping in the corner of a
screen all day, and the rows need width the page has and a 360px window does
not.

Drawn on a Tk canvas rather than in a browser so the widget is stdlib-only:
`pyproject.toml` carries zero dependencies and this must not be the change
that adds one. Every colour, breakpoint and pixel grid below is copied from
`dash/static/index.html` on purpose -- the two must read as the same object,
so when the page's ramp moves, this moves with it.

Numbers come from the dash server's `/api/windows`, never from the store
directly: the projection lives in `windows.py` behind that endpoint, and a
second reader would be a second answer to the same question.

A widget running old code cannot know it is old -- it would have to be
upgraded to notice the check that says so. The number that matters is the
dash's own: `/api/windows` carries the `server_version` of the *process*
that computed it, and `_check_version` (in `_fetch`) restarts that process,
once, when it disagrees with `__version__` (D60). The ↻ button stays the
manual escape hatch for everything this cannot catch -- a widget stuck on
old code with no dash to compare against, say.
"""

from __future__ import annotations

import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import __version__, store

BG = "#191714"
SURF = "#221e1a"
LINE = "#3a352e"
INK = "#ece7df"
DIM = "#8f887c"
CLAY = "#f4c7ad"
# l0 is "not yet burnt", l1..l4 the green -> red ramp. Same five as the page.
PX_LVL = ["#2b2a26", "#4f7a58", "#9aa04a", "#c8873c", "#bd4a3a"]

PX_DIGIT = [
    "111101101101111", "001001001001001", "111001111100111", "111001111001111",
    "101101111001001", "111100111001111", "111100111101111", "111001001001001",
    "111101111101111", "111101111001111",
]

# The flame, all three frames of the page's own animation, each on a 7x10
# grid, transcribed from the three box-shadow lists in index.html's `flame`
# (only the tip -- the top rows -- differs between them). A `Widget.after`
# timer cycles `self._flame_frame` through these at the page's own 140ms-a-
# frame rate, so the widget's flame flickers the same way the page's does.
FLAME_C = {
    "R": "#c9341f", "O": "#e0512a", "Y": "#f07f2c",
    "W": "#ffe6b0", "C": "#ffc65e", "D": "#a82a18",
}
FLAME_FRAMES = [
    [
        "...R...", "...R...", "..OOO..", "..OOO..", ".YYWYY.",
        ".YYWYY.", ".YYWYY.", "OOCCCOO", "OOCCCOO", ".DDDDD.",
    ],
    [
        "....R..", "...RR..", "..OOO..", "..OOO..", ".YYWYY.",
        ".YYWYY.", "YYYWYYY", "OOCCCOO", "OOCCCOO", ".DDDDD.",
    ],
    [
        "...R...", "..RR...", "..OOO..", ".OOOO..", ".YYWYY.",
        ".YYWYY.", ".YYWYY.", "OOCCCOO", "OOCCCOO", ".DDDDD.",
    ],
]

CELLS = 40
POLL_SECONDS = 20
# How long a spawned `dash` gets to bind the port before another attempt is
# allowed (or, past that window with the port still silent, before the
# error text stops crediting it as "starting" and calls it broken instead).
DASH_SPAWN_COOLDOWN = 30
# Consecutive spawns allowed to exit without ever answering a fetch, before
# giving up and naming the manual fix instead of retrying forever -- a dash
# that can never bind the port (bad install, a port nothing can free) must
# not turn into a respawn every `DASH_SPAWN_COOLDOWN` seconds for good.
DASH_SPAWN_ATTEMPTS = 2
# The pid file `dash/server.py`'s `serve()` writes on startup (and removes on
# clean shutdown), read here by the restart button so it can signal the exact
# process the widget's numbers are coming from -- never a process found by
# scanning ports or matching a command name.
DASH_PID_FILENAME = "dash.pid"
# Bounded wait, after signalling the old dash, for its port to stop
# answering -- roughly the same order as `_kill_and_wait`'s own ~2s budget in
# dash/server.py, with a little headroom since this also has to notice a
# SIGTERM that took a beat to land.
RESTART_WAIT_SECONDS = 3
RESTART_WAIT_STEP = 0.1
WIDTH = 470
HEIGHT = 210
# Every type size in the widget, in one place. They were inline literals two
# points smaller, which read as fine print on a retina panel. Raising them
# means the panel grows with them: WIDTH, HEIGHT and the footer column starts
# below are sized to what these fonts now measure.
HEAD_SIZE = 13   # the verdict headline; the close X sizes off it too
SUB_SIZE = 11    # the line under the headline
LABEL_SIZE = 10  # the small-caps label over a bar or a footer cell
VALUE_SIZE = 12  # the footer cells' numbers
# The collapsed panel's height: the week's fuse bar sits at top=52, height=14,
# so its bottom edge lands at 66; 10px of clearance below that gives 76 --
# face, a percentage-left line and the bar (plus the flame riding it) stay
# visible and nothing below them does. Not iconify()d: `_show` strips the
# titlebar with `overrideredirect`, and macOS will not reliably restore a
# borderless minimized window, so minimizing here means collapsing the panel
# in place instead.
MIN_HEIGHT = 76


def px_lvl(p: float) -> int:
    return 4 if p >= 95 else 3 if p >= 75 else 2 if p >= 45 else 1 if p > 0 else 0


def px_left_lvl(pl: float) -> int:
    return 4 if pl <= 10 else 3 if pl <= 25 else 2 if pl <= 55 else 1


def fmt_tok(n: float | None) -> str:
    """index.html's fmtTok, tier for tier -- including the B tier and the one
    decimal on K. An earlier version rounded K to whole thousands, so the page
    and the widget printed different numbers for the same field.
    """
    if n is None:
        return "—"
    a = abs(n)
    if a >= 1e9:
        return f"{n / 1e9:.1f}B"
    if a >= 1e6:
        return f"{n / 1e6:.1f}M"
    if a >= 1e3:
        return f"{n / 1e3:.1f}K"
    return str(round(n))


def fmt_usd(n: float | None) -> str:
    if n is None:
        return "—"
    neg, a = n < 0, abs(n)
    s = f"${round(a):,}" if a >= 1000 else f"${a:.2f}"
    return "-" + s if neg else s


def fmt_pct(p: float | None) -> str:
    return "—" if p is None else f"{p:.0f}%"


def _local(iso: str | None) -> datetime | None:
    """Parse an API timestamp and move it to this machine's clock.

    The server sends UTC (`...Z`), which `fromisoformat` reads as a UTC-aware
    datetime; `strftime` on that prints the UTC clock. The page converts to
    local (index.html's `new Date(iso).getHours()`), so a widget that printed
    the raw UTC named a reset hours off from the page's -- and, near midnight,
    the wrong weekday. Naive timestamps are already local and pass through.
    """
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return d.astimezone() if d.tzinfo is not None else d


def fmt_clock(iso: str | None) -> str:
    d = _local(iso)
    return "—" if d is None else d.strftime("%H:%M")


def fmt_day_clock(iso: str | None) -> str:
    """`TUE 15:41`, the page's own format for a moment days out.

    The time alone was a lie by omission here: the week runs out three or four
    days ahead, and a bare `15:41` under EMPTY AT reads as tonight. The page
    has always carried the weekday (index.html's DAY_UP); this now matches it.
    """
    d = _local(iso)
    return "—" if d is None else d.strftime("%a %H:%M").upper()


# Calm -> alarmed face, the same six-stop ladder as index.html's heroFuse,
# gated on cap percent left rather than the clock fallback so a capless
# widget never shows an alarmed face it hasn't earned. Kept in one obvious
# place (not duplicated per-branch in _verdict) so the page and the widget
# can only ever drift by someone forgetting to touch both.
FACE_LADDER = ((5, "🥵"), (10, "😰"), (15, "😟"), (25, "😬"))

# _verdict's exhaustion-branch edges -- the widget's own ladder, and the
# contract index.html's heroFuse (nearlyGone/nearlySpent/gettingTight, ~lines
# 414-416) now follows: 5, 12, 25. 25 is deliberately the same edge where
# FACE_LADDER's 😬 starts, so the verdict and the face begin together. If one
# side's ramp ever moves, this is the place to move it first.
VERDICT_EDGES = (5, 12, 25)  # almost gone / nearly spent / getting tight


def _face(pct_left: float | None) -> str:
    if pct_left is None:
        return "😐"
    for edge, glyph in FACE_LADDER:
        if pct_left <= edge:
            return glyph
    return "🙂"


def _cap_week_sub(left: float | None, week_left: float | None, tail: str) -> str:
    """Percent of the cap left and how much of the week remains -- the two
    figures a burn pace can actually be judged against. Replaces the old
    clock-lead/lag phrasing: not a metric that can be quantified or useful,
    per the page's own change. `tail` distinguishes the branches so the
    headline and this line never contradict each other.
    """
    parts = []
    if left is not None:
        parts.append(f"{left:.0f}% of the cap is left")
    if week_left is not None:
        parts.append(f"{week_left:.0f}% of the week to go")
    if not parts:
        return tail
    body = parts[0] if len(parts) == 1 else f"{parts[0]}, with {parts[1]}"
    return f"{body} {tail}" if tail else f"{body}."


def _fuse_top(clock_pct: float | None, used_pct: float, source: str) -> int:
    """The expanded panel's fuse bar top -- 82 normally, dropped 14px to 96
    when the clock and the flame would land within 7 points of each other.

    index.html's own fix for this exact collision (DECISIONS.md, 2026-08-06):
    drop the bar rather than move either mark -- the clock's stem just
    stretches to reach it (index.html:575, `Math.abs(clockPct - usedPct) <
    7`). Lifted out of `draw` so the rule can be tested without a live Tk
    canvas.
    """
    if clock_pct is not None and source == "cap" and abs(clock_pct - used_pct) < 7:
        return 96
    return 82


def _verdict(hero: dict, caps_known: bool, hook_ran: bool) -> tuple[str, str]:
    """The page's ladder, in its own order -- staleness first, then absolute
    exhaustion, then pace. Reordering it would let a 2%-left week be described
    as "comfortably inside" because the flame happens to trail the clock.
    """
    age = hero.get("reading_age_hours")
    if age is not None and age >= 12:
        return (f"Reading is {round(age)} hours old.", "Open /usage for a fresh one.")
    if not caps_known:
        return ("Collecting — not enough yet" if hook_ran else "Caps unknown.",
                "Open /usage once to light the fuse.")
    used = hero.get("used_pct")
    left = None if used is None else 100 - used
    clock_pct = hero.get("clock_pct")
    week_left = None if clock_pct is None else 100 - clock_pct
    behind = hero.get("behind")
    if left is not None and left <= VERDICT_EDGES[0]:
        return ("The week is almost gone.", f"{left:.0f}% of the cap is left, whatever the pace.")
    if left is not None and left < VERDICT_EDGES[1]:
        return ("Nearly spent.", f"{left:.0f}% of the cap is left.")
    if left is not None and left < VERDICT_EDGES[2]:
        return ("Getting tight.", f"{left:.0f}% of the cap is left.")
    if behind is not None and behind > 6:
        return ("Burning faster than it refills.", _cap_week_sub(left, week_left, ""))
    if behind is not None and behind > 0:
        return ("A little ahead of the clock.",
                _cap_week_sub(left, week_left, "— comfortable, worth a glance."))
    if behind is not None:
        return ("Comfortably inside the week.",
                _cap_week_sub(left, week_left, "— plenty of room."))
    return ("Tracking the week.", "No reset time yet, so no clock to compare.")


def _close_rect() -> tuple[float, float, float, float]:
    """Hit box for the top-right close 'X': a HEAD_SIZE-square inset from the
    panel's 2px border by half the shared 16px `pad` every row already uses.
    Derived from WIDTH/HEAD_SIZE rather than a pixel offset someone measured
    once, so it stays put if the panel's size or font ever changes.
    """
    pad, border = 16, 2
    margin = pad // 2
    side = HEAD_SIZE
    x1 = WIDTH - border - margin
    y0 = border + margin
    x0 = x1 - side
    y1 = y0 + side
    return x0, y0, x1, y1


def _hit_close(x: float, y: float) -> bool:
    """Whether a canvas point (e.g. a click) lands on the close 'X'."""
    x0, y0, x1, y1 = _close_rect()
    return x0 <= x <= x1 and y0 <= y <= y1


def _min_rect() -> tuple[float, float, float, float]:
    """Hit box for the minimize button: the same HEAD_SIZE square as the
    close 'X', sitting immediately to its left with an 8px gap between them.
    Derived from `_close_rect()` rather than its own WIDTH offset, so the two
    buttons can never drift apart.
    """
    cx0, cy0, cx1, cy1 = _close_rect()
    gap = 8
    x1 = cx0 - gap
    x0 = x1 - (cx1 - cx0)
    return x0, cy0, x1, cy1


def _hit_min(x: float, y: float) -> bool:
    """Whether a canvas point (e.g. a click) lands on the minimize button."""
    x0, y0, x1, y1 = _min_rect()
    return x0 <= x <= x1 and y0 <= y <= y1


def _restart_rect() -> tuple[float, float, float, float]:
    """Hit box for the restart '↻' button, immediately left of minimize --
    same HEAD_SIZE square, same 8px gap, derived from `_min_rect()` so all
    three buttons can never drift apart from one another.
    """
    mx0, my0, mx1, my1 = _min_rect()
    gap = 8
    x1 = mx0 - gap
    x0 = x1 - (mx1 - mx0)
    return x0, my0, x1, my1


def _hit_restart(x: float, y: float) -> bool:
    """Whether a canvas point (e.g. a click) lands on the restart button."""
    x0, y0, x1, y1 = _restart_rect()
    return x0 <= x <= x1 and y0 <= y <= y1


def _pid_path() -> Path:
    """Where `dash/server.py`'s `serve()` records its own pid + port --
    alongside the store's own data dir, so `CCMETRICS_DB` overrides it the
    same way it overrides the store's db file. Reuses `store.db_path()`
    rather than re-deriving that env-var lookup a second time.
    """
    return store.db_path().parent / DASH_PID_FILENAME


def _read_pid_file(path: Path) -> tuple[int, int] | None:
    """(pid, port) from the dash's own pid file, or None if it is missing,
    unreadable, or not the {"pid": int, "port": int} shape `serve()` writes.
    A malformed or absent file is never an error here -- it is exactly the
    "dash started some other way" case the restart button has to fall back
    from (see `Widget._restart_worker`).
    """
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    pid, port = body.get("pid"), body.get("port")
    if not isinstance(pid, int) or not isinstance(port, int):
        return None
    return pid, port


def _pid_alive(pid: int) -> bool:
    """Signal 0: no delivery, just an existence/permission check."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours -- still alive
    return True


def _dash_answers(port: int, timeout: float = 1.5) -> bool:
    """Same shape check as the dash's own `_probe_ccmetrics` (dash/server.py):
    GET /api/windows answers 200 with the JSON windows_payload emits. Anything
    else -- wrong shape, an error, a refused connection -- means whatever is
    on this port is not (or is no longer) a ccmetrics dash. This, plus
    `_pid_alive`, is the pid file's pid and port confirmed together before a
    restart is ever allowed to signal it.
    """
    url = f"http://127.0.0.1:{port}/api/windows"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return False
            body = json.loads(r.read().decode())
    except Exception:
        return False
    return isinstance(body, dict) and "scope" in body and "caps_known" in body


def _wait_port_silent(port: int) -> None:
    """Bounded ~RESTART_WAIT_SECONDS poll for a just-signalled dash's port to
    stop answering, so the respawn that follows does not race a process
    still tearing down. Always called from `Widget._restart_worker`, off the
    main thread, so this sleeping loop never blocks the Tk event loop.
    """
    deadline = time.monotonic() + RESTART_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _dash_answers(port, timeout=0.3):
            return
        time.sleep(RESTART_WAIT_STEP)


def restart_if_outdated(port: int = 7433, timeout: float = 1.5) -> None:
    """D60's other reader: the plain `ccmetrics` summary never opens the
    widget, so `Widget._check_version`'s poll-driven check never runs for
    it. Same stale-dash problem (module docstring), same D59 pid-file
    restart, called once per CLI invocation instead of on a timer.

    Every failure mode here -- nothing listening, no pid file, a dead or
    foreign pid, the kill itself failing -- is swallowed: this must never
    keep the summary the user actually asked for from printing. The short
    `timeout` bounds the one network round trip; the kill+respawn below is
    fire-and-forget, so a slow-to-die old dash does not hold up the CLI.
    """
    try:
        url = f"http://127.0.0.1:{port}/api/windows"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return
            body = json.loads(r.read().decode())
        if not isinstance(body, dict) or body.get("server_version") == __version__:
            return
        info = _read_pid_file(_pid_path())
        if info is None:
            return
        pid, pid_port = info
        if pid_port != port or not _pid_alive(pid):
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        _wait_port_silent(pid_port)
        subprocess.Popen(
            [sys.executable, "-m", "ccmetrics", "dash", "--no-open", "--port", str(pid_port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except Exception:
        return


class Widget:
    """The window itself. Owns the canvas and the poll timer, nothing else."""

    def __init__(self, tk, port: int, scope: str | None = None) -> None:
        self.tk = tk
        self.port = port
        self.scope = scope
        self.data: dict | None = None
        self.error: str | None = None
        self._closing = False
        self._minimized = False
        self._draw_timer: str | None = None
        self._poll_timer: str | None = None
        # Set the first time `_fetch` finds nothing on `self.port`; see
        # `_maybe_spawn_dash` and `_fetch_error_text`. `_dash_fails` counts
        # spawns that exited without ever answering a fetch and resets to 0
        # the moment a fetch succeeds.
        self._dash_proc: subprocess.Popen | None = None
        self._dash_spawn_at: float | None = None
        self._dash_fails = 0
        # True while `_restart_worker` is running off-thread; `_poll_restart`
        # watches it on the main thread to know when to draw the outcome.
        self._restarting = False
        # D60: set the first (and only) time `_check_version` drives a
        # restart for a `server_version` mismatch, so a dash that is a
        # genuinely different install (not just stale) gets kicked once,
        # never on every poll. `_version_notice` is the text `draw` shows
        # once that one attempt still leaves the versions disagreeing.
        self._version_restarted = False
        self._version_notice: str | None = None
        # The flame's own animation state: which of FLAME_FRAMES is current,
        # a timer that just cycles it, and the (cx, bottom) `draw()` last
        # placed a flame at -- None when no flame is showing (no known cap) --
        # so the timer can redraw just the flame without a full repaint.
        self._flame_frame = 0
        self._flame_timer: str | None = None
        self._flame_pos: tuple[float, float] | None = None

        root = tk.Tk()
        root.title("ccmetrics")
        root.configure(bg=BG)
        root.geometry(f"{WIDTH}x{HEIGHT}+80+80")
        self.root = root

        self.canvas = tk.Canvas(
            root, width=WIDTH, height=HEIGHT, bg=BG, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        # Held on the instance: Tk drops an image with no live reference, and
        # a garbage-collected icon leaves the default rocket in the Dock.
        self._iconimg = self._icon()
        root.iconphoto(True, self._iconimg)

        self._drag = (0, 0)
        self.canvas.bind("<Button-1>", self._grab)
        self.canvas.bind("<B1-Motion>", self._move)
        root.bind("<Escape>", lambda _e: self._shutdown())
        root.bind("<Double-Button-1>", lambda _e: self._shutdown())

        # The close 'X': tag_bind lives on the tag name, not an item id, so
        # it survives every draw()'s delete("all") -- the next redraw just
        # hands the same tag to a fresh item. The handler defers to
        # after_idle rather than calling _shutdown directly -- see _shutdown
        # for why destroying from inside this dispatch crashes.
        self.canvas.tag_bind("close", "<Button-1>", lambda _e: root.after_idle(self._shutdown))
        self.canvas.tag_bind("close", "<Enter>", lambda _e: self._close_hover(True))
        self.canvas.tag_bind("close", "<Leave>", lambda _e: self._close_hover(False))

        # The minimize button: same tag-bind pattern as close, so it too
        # survives every draw()'s delete("all"). Same after_idle deferral as
        # close's handler, for the same reason -- _toggle_min's draw() does a
        # delete("all") that would otherwise free the very item Tk is still
        # dispatching this click from.
        self.canvas.tag_bind("min", "<Button-1>", lambda _e: root.after_idle(self._toggle_min))
        self.canvas.tag_bind("min", "<Enter>", lambda _e: self._min_hover(True))
        self.canvas.tag_bind("min", "<Leave>", lambda _e: self._min_hover(False))

        # The restart '↻' button: same tag-bind pattern as close and min, so
        # it too survives every draw()'s delete("all"). Same after_idle
        # deferral, for the same reason -- _restart_dash's own draw() must
        # not run while Tk is still dispatching the click that triggered it.
        self.canvas.tag_bind("restart", "<Button-1>", lambda _e: root.after_idle(self._restart_dash))
        self.canvas.tag_bind("restart", "<Enter>", lambda _e: self._restart_hover(True))
        self.canvas.tag_bind("restart", "<Leave>", lambda _e: self._restart_hover(False))

    def _icon(self):
        """The Dock tile: the same pixel flame the fuse burns, on the panel's
        own dark square. Built at runtime rather than shipped as a file --
        there is no icon asset in the repo (the page's favicon is a 404), and
        adding one would mean a binary in a package that has none.

        `put` takes a row-major list of colour rows, so the flame is scaled by
        repeating each pixel `s` times across and each row `s` times down.
        """
        frame = FLAME_FRAMES[0]
        size, grid = 64, len(frame)
        s = 5  # 7x10 flame at 5x -> 35x50 inside a 64px tile
        pad_x = (size - len(frame[0]) * s) // 2
        pad_y = (size - grid * s) // 2
        rows = [[SURF] * size for _ in range(size)]
        for r, row in enumerate(frame):
            for c, ch in enumerate(row):
                if ch == ".":
                    continue
                for dy in range(s):
                    for dx in range(s):
                        rows[pad_y + r * s + dy][pad_x + c * s + dx] = FLAME_C[ch]
        img = self.tk.PhotoImage(width=size, height=size)
        img.put(" ".join("{" + " ".join(row) + "}" for row in rows))
        return img

    def _grab(self, e) -> None:
        if _hit_close(e.x, e.y) or _hit_min(e.x, e.y) or _hit_restart(e.x, e.y):
            # The X, the minimize button and the restart button all handle
            # their own clicks; this must not start a drag. Clear the anchor
            # rather than just skipping the update -- a stale one would let
            # a press-on-a-button-then-drag-off jump the window by the delta
            # from whatever the *previous* click left behind.
            self._drag = None
            return
        self._drag = (e.x, e.y)

    def _move(self, e) -> None:
        if self._drag is None:
            return
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _close_hover(self, on: bool) -> None:
        self.canvas.itemconfigure("close_bg", fill=LINE if on else SURF)
        self.canvas.itemconfigure("close_x", fill=INK if on else DIM)

    def _min_hover(self, on: bool) -> None:
        self.canvas.itemconfigure("min_bg", fill=LINE if on else SURF)
        self.canvas.itemconfigure("min_mark", fill=INK if on else DIM)

    def _restart_hover(self, on: bool) -> None:
        self.canvas.itemconfigure("restart_bg", fill=LINE if on else SURF)
        self.canvas.itemconfigure("restart_mark", fill=INK if on else DIM)

    def _toggle_min(self) -> None:
        if self._closing:
            return
        self._minimized = not self._minimized
        # Canvas and toplevel both, and in that order: pack() sizes the
        # toplevel off the canvas's own requested size, so resizing only the
        # geometry leaves the canvas still asking for the old height and the
        # window snapping back to it on some window managers.
        panel_h = MIN_HEIGHT if self._minimized else HEIGHT
        self.canvas.configure(height=panel_h)
        # Size only, no +x+y -- the window keeps wherever the user dragged it.
        self.root.geometry(f"{WIDTH}x{panel_h}")
        self.draw()

    def _shutdown(self) -> None:
        """The one teardown path -- the X, Escape and the double-click all
        land here rather than each calling `root.destroy()` itself.

        The X used to destroy the toplevel straight from its own canvas-item
        binding. Tk was still inside that item's event dispatch when the
        canvas -- and the item -- vanished under it, which is what macOS saw
        as "python quit unexpectedly" rather than a clean exit. Escape never
        crashed because it is bound on the toplevel, not an item, so nothing
        is dispatching out of the thing being destroyed. `after_idle` in the
        close binding gets the X the same safety: it defers this call until
        Tk has finished dispatching the click.

        `_closing` is set first and cancels the pending `after` timers before
        destroying: `draw`/`_poll` can still be sitting in the event queue
        when this runs, and firing into a canvas that `destroy()` just freed
        is the same crash by a different path. The fetch thread itself never
        touches Tk -- it only assigns to `self.data`/`self.error` -- but it
        also checks `_closing` so a fetch that lands after teardown does not
        queue another redraw. Do not simplify this back into a bare
        `root.destroy()`.

        No `WM_DELETE_WINDOW` binding: `_show` strips the window frame with
        `overrideredirect(True)` before the widget is ever visible, so there
        is no titlebar close button for the window manager to route here.
        """
        if self._closing:
            return
        self._closing = True
        if self._draw_timer is not None:
            self.root.after_cancel(self._draw_timer)
            self._draw_timer = None
        if self._poll_timer is not None:
            self.root.after_cancel(self._poll_timer)
            self._poll_timer = None
        if self._flame_timer is not None:
            self.root.after_cancel(self._flame_timer)
            self._flame_timer = None
        self.root.destroy()

    # ---- data ----------------------------------------------------------

    def _url(self) -> str:
        url = f"http://127.0.0.1:{self.port}/api/windows"
        return url + f"?project={urllib.parse.quote(self.scope)}" if self.scope else url

    def _fetch(self) -> None:
        """Runs off the main thread -- Tk is not thread-safe, so this only ever
        assigns to `self.data`/`self.error`/`self._dash_proc`/`self._dash_spawn_at`/
        `self._dash_fails` and lets the `after` timer redraw. `subprocess.Popen`
        and the plain attribute writes in `_maybe_spawn_dash` are as
        thread-safe as the `self.data`/`self.error` writes already made here.

        Checks `_closing` twice: once up front, so a fetch that never even
        starts after `_shutdown` skips the request outright, and again after
        `urlopen` returns, because a fetch already inside that call when the
        X is clicked passed the first check seconds earlier and would still
        write into `self.data`/`self.error` (and, worse, spawn a dash for a
        widget that is going away) on the way out otherwise.
        """
        if self._closing:
            return
        body, failed, conn_refused = None, False, False
        try:
            with urllib.request.urlopen(self._url(), timeout=4) as r:
                body = json.loads(r.read().decode())
        except urllib.error.URLError as exc:
            # `reason` is the raw OSError urlopen was retrying underneath --
            # a bare ConnectionRefusedError means nothing is listening on
            # the port at all, distinct from a live server timing out below.
            failed, conn_refused = True, isinstance(exc.reason, ConnectionRefusedError)
        except (OSError, ValueError, TimeoutError):
            failed = True
        if self._closing:
            return
        if not failed:
            self.data = body
            self.error = None
            self._dash_fails = 0  # a live dash answered; forgive whatever came before
            self._check_version()
            return
        # Only a refused connection is a "nothing is serving" case worth
        # auto-starting a dash for -- a live server that merely timed out
        # is left alone.
        if conn_refused:
            self._maybe_spawn_dash()
        self.error = self._fetch_error_text()

    def _maybe_spawn_dash(self) -> None:
        """Connection refused on `self.port` means no dash is serving it --
        start one rather than leave the widget polling a dead port until the
        user notices and runs `ccmetrics dash` by hand.

        `python -m ccmetrics` is used over the `ccmetrics` console script:
        `__main__.py` already exists for exactly this fallback (see its own
        docstring), so this adds no new packaging surface. `--port` is
        passed through since the `dash` subparser already takes one --
        without it a widget pointed at a non-default port would spawn a
        dash on the wrong one. `start_new_session=True` so the dash outlives
        this widget process if the widget is closed first.

        Guarded three ways against respawn storms: `_dash_proc.poll() is None`
        skips a second spawn while the first is still alive (started but not
        yet bound, or bound and serving happily); the cooldown on
        `_dash_spawn_at` covers a spawn that already exited (crashed) or the
        gap right after Popen returns but before the new process has bound
        the port; and `_dash_fails` reaching `DASH_SPAWN_ATTEMPTS` stops
        spawning altogether once a dash has proved it cannot stay up --
        without that cap a dash that can never bind the port would get
        relaunched every `DASH_SPAWN_COOLDOWN` seconds forever. A dead
        `_dash_proc` is dropped the same call it is counted in, so the
        strike is tied to one spawn, not to how many polls land while its
        cooldown is still running -- do not hold onto the handle past the
        count (e.g. to read its exit code) without re-checking that.
        """
        if self._dash_proc is not None and self._dash_proc.poll() is None:
            return
        if self._dash_proc is not None:
            # The previous spawn is confirmed dead (poll() above returned an
            # exit code, not None) -- one more strike against this port.
            # Cleared immediately so a still-cooling-down poll that sees the
            # same corpse again does not count it a second time: without
            # this, one dead spawn gets re-counted on every poll until
            # DASH_SPAWN_COOLDOWN passes, tripping DASH_SPAWN_ATTEMPTS on
            # far fewer than that many actual spawns.
            self._dash_proc = None
            self._dash_fails += 1
        if self._dash_fails >= DASH_SPAWN_ATTEMPTS:
            return
        now = time.monotonic()
        if self._dash_spawn_at is not None and now - self._dash_spawn_at < DASH_SPAWN_COOLDOWN:
            return
        self._dash_spawn_at = now
        self._dash_proc = subprocess.Popen(
            [sys.executable, "-m", "ccmetrics", "dash", "--no-open", "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )

    def _fetch_error_text(self) -> str:
        """The "starting..." text holds for as long as a spawn from
        `_maybe_spawn_dash` is within its cooldown window and has not yet
        used up its `DASH_SPAWN_ATTEMPTS`, so the reader sees a fuse about to
        resolve itself rather than a dead end. Once attempts run out --
        whether the cooldown has simply lapsed on a dash still booting, or
        `_maybe_spawn_dash` has already given up -- back to naming the
        manual fix, which is reachable again because `_dash_fails` caps the
        retries instead of letting them run forever.
        """
        if self._dash_fails < DASH_SPAWN_ATTEMPTS:
            spawned_recently = (
                self._dash_spawn_at is not None
                and time.monotonic() - self._dash_spawn_at < DASH_SPAWN_COOLDOWN
            )
            if spawned_recently:
                return f"starting dash on :{self.port}…"
        return f"nothing on :{self.port} — run  ccmetrics dash"

    # ---- restart ---------------------------------------------------------

    def _restart_dash(self) -> None:
        """↻: restart the *dash server* the widget reads from, never the
        widget itself -- the window sits wherever the user dragged it, and
        re-execing this process would lose that spot. Every number on the
        panel comes from that server's own `/api/windows` (module
        docstring), which holds old code in memory until its process is
        killed and a fresh one takes its place; this drives exactly that.

        Only sets the transient placeholder and hands off to the worker
        thread -- the pid read, the signal, the bounded wait and the
        respawn all happen there, off this (the Tk) thread.

        Also guarded on `self._restarting`: a second click while one is
        already in flight must not start a second worker thread -- two
        overlapping kill/spawn sequences could otherwise have the first
        one's `finally` flip the flag off (and this method draw "done")
        while the second is still off tearing down or bringing up a dash.
        """
        if self._closing or self._restarting:
            return
        self.data = None
        self.error = "restarting dash…"
        self._restarting = True
        self.draw()
        threading.Thread(target=self._restart_worker, daemon=True).start()
        self._poll_restart()

    def _poll_restart(self) -> None:
        """Watches `self._restarting` every 200ms on the main thread rather
        than blocking it -- the bounded wait for the old dash's port to go
        quiet happens inside `_restart_worker`, off this thread entirely;
        this only notices when that thread flips the flag back off, then
        draws once with whatever it left behind.
        """
        if self._closing:
            return
        if self._restarting:
            self.root.after(200, self._poll_restart)
            return
        self.draw()

    def _restart_worker(self) -> None:
        """Runs off the main thread -- same discipline as `_fetch`: only
        ever assigns to self.data/self.error/self._dash_proc/
        self._dash_spawn_at/self._dash_fails/self._restarting, never touches
        the canvas. `self._closing` is re-checked at every step a user could
        have closed the widget mid-restart, so a slow kill+respawn can
        never resurrect (or spawn) a dash after the window is gone.
        """
        try:
            if self._closing:
                return
            info = _read_pid_file(_pid_path())
            if info is None:
                # No pid file: dash was started some other way (or hasn't
                # written one yet). Never hunt for a process to kill by name
                # or by scanning ports -- a plain refetch is all that is
                # safe, and the placeholder says so rather than claiming a
                # restart that never happened.
                self.error = f"no pid file — refreshing :{self.port}…"
                self._fetch()
                return
            pid, port = info
            if _pid_alive(pid) and _dash_answers(port):
                try:
                    os.kill(pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
                else:
                    _wait_port_silent(port)
            # else: the pid is dead, or alive but not answering as a
            # ccmetrics dash on the recorded port -- a stale or unrelated
            # pid file must never be signalled.
            if self._closing:
                return
            # A restart the user explicitly asked for must not be refused by
            # the D54 cooldown/strike cap meant to stop silent auto-spawns --
            # nor by a stale `_dash_proc` handle from an earlier auto-spawn:
            # `_maybe_spawn_dash` skips spawning while that handle's own
            # `poll()` still reads as alive, which can otherwise race the
            # kill+wait above (the port going quiet does not guarantee
            # `poll()` has observed the exit at this exact instant). Cleared
            # here rather than trusted to that race resolving in time.
            self._dash_spawn_at = None
            self._dash_fails = 0
            self._dash_proc = None
            self._maybe_spawn_dash()
            self._fetch()
        finally:
            self._restarting = False

    def _check_version(self) -> None:
        """D60: runs inside `_fetch`, off the main thread -- same discipline
        as `_maybe_spawn_dash`: only plain attribute writes and starting a
        thread, nothing that touches the canvas (a `draw()` call from here
        would race the one `_poll`'s own `after` timer already has queued).

        `self.data["server_version"]` is the *dash process's* own
        `__version__`, never this widget's -- an upgrade replaces the file
        on disk but not a dash already running (module docstring), so this
        comparison is the only way the widget can tell. A missing key (a
        dash too old to report one at all) reads as a mismatch the same way
        a different value does.

        Reuses the D59 pid-file restart (`_restart_worker`) rather than a
        second mechanism, through the same `self._restarting` guard a ↻
        click uses, so an automatic and a manual restart can never race.
        `self._version_restarted` caps this to one attempt per mismatch --
        two different ccmetrics installs on one machine, each pinned to its
        own version, would otherwise restart on every single poll forever.
        Once that attempt is spent and a later fetch still disagrees,
        `_version_notice` says so instead of trying again; `draw` shows it
        in place of the sub-line.
        """
        if self.data is None or self._restarting:
            return
        server_version = self.data.get("server_version")
        if server_version == __version__:
            self._version_notice = None
            return
        if self._version_restarted:
            self._version_notice = f"dash is v{server_version or 'unknown'}, this is v{__version__}"
            return
        self._version_restarted = True
        self._restarting = True
        threading.Thread(target=self._restart_worker, daemon=True).start()

    def _poll(self) -> None:
        if self._closing:
            return
        threading.Thread(target=self._fetch, daemon=True).start()
        self._draw_timer = self.root.after(400, self.draw)
        self._poll_timer = self.root.after(POLL_SECONDS * 1000, self._poll)

    def _flame_tick(self) -> None:
        """Cycles `self._flame_frame` at the page's own rate -- 0.42s over
        three frames is 140ms each -- and redraws just the flame at its last
        known position, rather than a full `draw()`, so the fuse bar, the
        clock and the rest of the panel do not repaint 7x a second along
        with it.

        Re-arms unconditionally, for the widget's life -- unlike the page's
        CSS `steps(3)` keyframe, which the browser composites and pauses
        itself, this keeps firing (and, once a flame is showing, keeps
        deleting/recreating its ~24 rects) even while `self._flame_pos` is
        `None` or the panel sits idle all day. Accepted cost of a stdlib-only
        Tk port having no such backstop of its own.
        """
        if self._closing:
            return
        self._flame_frame = (self._flame_frame + 1) % len(FLAME_FRAMES)
        if self._flame_pos is not None:
            self.canvas.delete("flame")
            self._flame(*self._flame_pos)
        self._flame_timer = self.root.after(140, self._flame_tick)

    # ---- drawing -------------------------------------------------------

    def _px(self, x: float, y: float, w: float, h: float, fill: str,
            tags: tuple[str, ...] = ()) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill=fill, outline="", tags=tags)

    def _draw_close(self) -> None:
        """Top-right close 'X', dim until hovered (`_close_hover`). Tagged
        'close' for the click/hover bindings set up once in `__init__`, plus
        'close_bg'/'close_x' so hover can recolour box and mark separately.
        """
        x0, y0, x1, y1 = _close_rect()
        self._px(x0, y0, x1 - x0, y1 - y0, SURF, tags=("close", "close_bg"))
        m = 3
        self.canvas.create_line(x0 + m, y0 + m, x1 - m, y1 - m,
                                fill=DIM, width=2, tags=("close", "close_x"))
        self.canvas.create_line(x0 + m, y1 - m, x1 - m, y0 + m,
                                fill=DIM, width=2, tags=("close", "close_x"))

    def _draw_min(self) -> None:
        """Minimize button, immediately left of the close 'X'. Tagged 'min'
        for the click/hover bindings set up once in `__init__`, plus
        'min_bg'/'min_mark' so hover can recolour box and mark separately.

        The mark says which way the button goes: a bar across the lower
        third when expanded (the standard minimize glyph), a hollow square
        when already collapsed (restore).
        """
        x0, y0, x1, y1 = _min_rect()
        self._px(x0, y0, x1 - x0, y1 - y0, SURF, tags=("min", "min_bg"))
        m, w = 3, 2
        if self._minimized:
            # A hollow square built from four thin `_px` strips rather than
            # `create_rectangle`'s outline: that item recolours on hover via
            # `-outline`, not `-fill`, and the expanded mark below is a line
            # that only has `-fill` -- two options on one tag, one of them
            # always wrong. Four rects keep every 'min_mark' item on `-fill`,
            # so `_min_hover` stays a plain itemconfigure(fill=...) like close.
            ix0, iy0, ix1, iy1 = x0 + m, y0 + m, x1 - m, y1 - m
            for rx, ry, rw, rh in ((ix0, iy0, ix1 - ix0, w), (ix0, iy1 - w, ix1 - ix0, w),
                                   (ix0, iy0, w, iy1 - iy0), (ix1 - w, iy0, w, iy1 - iy0)):
                self._px(rx, ry, rw, rh, DIM, tags=("min", "min_mark"))
        else:
            y = y0 + (y1 - y0) * 2 / 3
            self._px(x0 + m, y - w / 2, (x1 - x0) - 2 * m, w, DIM, tags=("min", "min_mark"))

    def _draw_restart(self) -> None:
        """Restart button, immediately left of minimize. Tagged 'restart'
        for the click/hover bindings set up once in `__init__`, plus
        'restart_bg'/'restart_mark' so hover can recolour box and mark
        separately, same split as close and min.

        The mark is the '↻' glyph itself rather than a pixel-grid drawing --
        unlike the flame and the digits, this has no page-side pixel art to
        stay in lockstep with, so there is nothing gained by hand-drawing it.
        """
        x0, y0, x1, y1 = _restart_rect()
        self._px(x0, y0, x1 - x0, y1 - y0, SURF, tags=("restart", "restart_bg"))
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        self.canvas.create_text(cx, cy, text="↻", fill=DIM, font=("Menlo", HEAD_SIZE),
                                tags=("restart", "restart_mark"))

    def _flame(self, cx: float, bottom: float) -> None:
        """Centres on `cx`, sits bottom-down from `bottom`. Draws whichever
        of FLAME_FRAMES `self._flame_frame` is currently on, tagged 'flame'
        so `_flame_tick` can erase and redraw just this without touching the
        rest of the panel. `draw()` records the position it called this with
        on `self._flame_pos` -- the tick has no other way to know it.
        """
        s = 3  # 7x10 grid at 3px -> a 21x30 flame
        frame = FLAME_FRAMES[self._flame_frame]
        left = cx - (len(frame[0]) * s) / 2
        top = bottom - len(frame) * s
        for r, row in enumerate(frame):
            for c, ch in enumerate(row):
                if ch != ".":
                    self._px(left + c * s, top + r * s, s, s, FLAME_C[ch], tags=("flame",))

    def _clock(self, cx: float, top: float, fuse_top: float) -> None:
        """23 columns wide at a 2px pitch, digits on a 4-column pitch. Drawn
        case -> face -> digits, the reverse of the page's shadow order, because
        a canvas paints later shapes on top where box-shadow paints earlier
        ones on top. Same picture, opposite order.
        """
        s = 2
        left = cx - (23 * s) / 2
        self._px(left, top, 23 * s, 11 * s, DIM)
        self._px(left + s, top + s, 21 * s, 9 * s, INK)
        now = datetime.now()
        text = f"{now.hour:02d}{now.minute:02d}"
        for glyph_i, col in enumerate((3, 7, 13, 17)):
            glyph = PX_DIGIT[int(text[glyph_i])]
            for gr in range(5):
                for gc in range(3):
                    if glyph[gr * 3 + gc] == "1":
                        self._px(left + (col + gc) * s, top + (gr + 3) * s, s, s, BG)
        for row in (4, 6):  # the colon, between the two pairs
            self._px(left + 11 * s, top + row * s, s, s, BG)
        # feet, and the stem down to the bar it marks
        for foot in (3, 5, 17, 19):
            self._px(left + foot * s, top + 11 * s, s, s, DIM)
        self._px(cx - 1, top + 12 * s, 2, fuse_top - (top + 12 * s), INK)

    def _bar(self, top: float, height: float, pct: float, graded: bool,
             ghost_to: float | None = None, fuse: bool = False) -> None:
        """One 40-cell bar. `graded` walks the green -> red ramp.

        `fuse` picks WHICH ramp, and the page draws these two the same way:
        a fuse grades by how far into its own fill each cell sits, so the
        week burns calm at the start and alarming at the flame. Every other
        bar takes ONE colour for the whole fill, from how much room is left
        -- grading those made a healthy 20% run green-to-red across itself
        and read as an emergency. Ungraded fills flat neutral, which is what
        a bar with no cap behind it is entitled to claim.

        `ghost_to` dims the cells between the fill and a projection, so the
        block bar can show where it is heading without drawing that stretch
        as though it were already spent.
        """
        pad, gap = 16, 1
        cell_w = (WIDTH - pad * 2 - gap * (CELLS - 1)) / CELLS
        flat = PX_LVL[px_left_lvl(100 - pct)]
        for i in range(CELLS):
            at = (i / CELLS) * 100
            if at < pct:
                if not graded:
                    fill = LINE
                elif fuse:
                    fill = PX_LVL[px_lvl(round((at / max(pct, 1)) * 100))]
                else:
                    fill = flat
            elif ghost_to is not None and at < ghost_to:
                fill = LINE
            else:
                fill = PX_LVL[0]
            self._px(pad + i * (cell_w + gap), top, cell_w, height, fill)

    def _block(self, top: float, block: dict | None, caps_known: bool) -> None:
        """The 5-hour window: how much of its own cap is gone, and where the
        current pace lands it by the time it refills.

        Separate from the fuse on purpose -- they answer different questions.
        The week's fuse says whether the month's work fits; this says whether
        the next hour does. Both matter mid-session, which is why the widget
        carries both bars rather than one.
        """
        label_c, value_c = DIM, INK
        if not block:
            self.canvas.create_text(16, top, text="5H BLOCK", anchor="w",
                                    fill=label_c, font=("Menlo", LABEL_SIZE))
            self.canvas.create_text(WIDTH - 16, top, text="—", anchor="e",
                                    fill=label_c, font=("Menlo", LABEL_SIZE))
            self._bar(top + 12, 12, 0, False)
            return

        pct = block.get("pct_of_cap")
        lands = block.get("lands_at_pct")
        known = caps_known and pct is not None
        # "BACK AT" read as though the block returned rather than reset. The
        # block empties and refills at that instant, so RESETS is the word.
        head = f"5H BLOCK · RESETS {fmt_clock(block.get('ends_at'))}"
        # Only claim a landing point when it is ahead of where the block
        # already stands: a projection that has fallen behind the actual fill
        # is stale arithmetic, and drawing it would put the mark inside the
        # burnt stretch where it reads as a cap line.
        show_lands = known and lands is not None and lands > pct
        tail = (fmt_pct(pct) + (f" → {fmt_pct(lands)}" if show_lands else "")) if known else "no cap known"

        self.canvas.create_text(16, top, text=head, anchor="w", fill=label_c, font=("Menlo", LABEL_SIZE))
        self.canvas.create_text(WIDTH - 16, top, text=tail, anchor="e",
                                fill=value_c if known else label_c, font=("Menlo", LABEL_SIZE))
        self._bar(top + 12, 12, pct if known else 0, known,
                  ghost_to=lands if show_lands else None)
        if show_lands:
            pad = 16
            x = pad + (WIDTH - pad * 2) * min(lands, 100) / 100
            self._px(x - 1, top + 9, 2, 18, CLAY)

    def draw(self) -> None:
        if self._closing:
            return
        # The panel's current height -- MIN_HEIGHT when collapsed, HEIGHT
        # otherwise -- pulled once so the background and every border strip
        # draw against whichever one is actually on screen.
        panel_h = MIN_HEIGHT if self._minimized else HEIGHT
        self.canvas.delete("all")
        self._px(0, 0, WIDTH, panel_h, SURF)
        for x, y, w, h in ((0, 0, WIDTH, 2), (0, panel_h - 2, WIDTH, 2),
                           (0, 0, 2, panel_h), (WIDTH - 2, 0, 2, panel_h)):
            self._px(x, y, w, h, LINE)
        self._draw_close()
        self._draw_min()
        self._draw_restart()

        if self.data is None:
            self._flame_pos = None
            self.canvas.create_text(
                WIDTH / 2, panel_h / 2, text=self.error or "reading the week…",
                fill=DIM, font=("Menlo", HEAD_SIZE),
            )
            return

        w = self.data
        hero = w.get("hero") or {}
        caps_known = bool(w.get("caps_known"))
        used = hero.get("used_pct")
        clock_pct = hero.get("clock_pct")

        # Same three-way source as the page: a known cap gives the real ramp,
        # a bare clock reading fills neutral to the clock's own position, and
        # neither gives an empty bar. The flame only ever rides a real cap --
        # drawing it against a clock fill would claim a burn point nobody knows.
        if caps_known and used is not None and clock_pct is not None:
            used_pct, source = used, "cap"
        elif clock_pct is not None:
            used_pct, source = clock_pct, "clock"
        else:
            used_pct, source = 0.0, "none"

        verdict, sub = _verdict(hero, caps_known, bool(w.get("hook_ran")))
        pct_left = (100 - used) if caps_known and used is not None else None
        # Face size scales off the headline's own font size rather than a
        # fixed pixel value, so the two move together if the headline text
        # size ever changes.
        face_size = HEAD_SIZE * 2
        head_y, sub_y = 20, 38  # the headline and sub-line rows' own y
        # The page centres the face against the whole headline-plus-sub
        # block (heroFuse's flex row, align-items:center). Here that block
        # is exactly these two text rows, so the face's y is their midpoint
        # -- derived from head_y/sub_y rather than a third, separately
        # measured pixel that could drift out of line with either row.
        # Collapsed has no headline/sub-line row to centre against, and the
        # bar (with the now-30px flame riding its bottom edge) sits lower
        # than expanded's -- 18 keeps the face and the '% left' line clear
        # of the flame's tip instead of inheriting expanded's lower face_y.
        face_y = 18 if self._minimized else (head_y + sub_y) / 2
        self.canvas.create_text(16, face_y, text=_face(pct_left), anchor="w",
                                fill=CLAY, font=("Menlo", face_size))
        text_x = 16 + face_size + 8

        if self._minimized:
            # Collapsed: face, a short percentage-left line and the week's
            # fuse bar -- no headline, no sub-line, nothing below the bar.
            left_text = f"{round(pct_left)}% left" if pct_left is not None else sub
            self.canvas.create_text(text_x, face_y, text=left_text, anchor="w",
                                    fill=INK, font=("Menlo", HEAD_SIZE))
            self._bar(52, 14, used_pct, source == "cap", fuse=True)
            if source == "cap":
                pad = 16
                bar_l, bar_w = pad, WIDTH - pad * 2
                self._flame_pos = (bar_l + bar_w * used_pct / 100, 52 + 14)
                self._flame(*self._flame_pos)
            else:
                self._flame_pos = None
            return

        self.canvas.create_text(text_x, head_y, text=verdict.upper(), anchor="w", fill=INK,
                                font=("Menlo", HEAD_SIZE))
        # D60: a still-mismatched-after-one-restart dash overrides the
        # sub-line rather than adding a second row -- see `_check_version`.
        self.canvas.create_text(text_x, sub_y, text=self._version_notice or sub, anchor="w",
                                fill=DIM, font=("Menlo", SUB_SIZE))

        # No label over the week's bar. One would have to clear the clock,
        # which can sit at any point along the row, and stacking it above the
        # clock put three tight lines over a tall gap. The verdict above names
        # the week already, and only the block bar below needs saying which.
        bar_h, pad = 18, 16
        # See _fuse_top: the bigger, s=3 flame's box now reaches the clock's
        # stem at the old fixed 82, so this needs the same <7-point guard the
        # page already carries. The divider below (fixed at y=116) and the
        # block bar under it do NOT shift with the drop -- unlike the page's
        # flow layout, everything below here is an absolute y -- so the <7
        # case renders a tighter gap to the divider (2px instead of 16) than
        # every other reading. Deliberate: matches the page's own "never move
        # a mark" fix rather than growing the panel for a rare case.
        fuse_top = _fuse_top(clock_pct, used_pct, source)
        self._bar(fuse_top, bar_h, used_pct, source == "cap", fuse=True)

        bar_l, bar_w = pad, WIDTH - pad * 2
        if clock_pct is not None:
            self._clock(bar_l + bar_w * clock_pct / 100, 48, fuse_top)
        if source == "cap":
            self._flame_pos = (bar_l + bar_w * used_pct / 100, fuse_top + bar_h)
            self._flame(*self._flame_pos)
        else:
            self._flame_pos = None

        self._px(pad, 116, bar_w, 2, LINE)
        self._block(126, w.get("current_block"), caps_known)

        # The page's four footer cells, field for field (index.html's burntLine,
        # wick, runsOut, RATE). They are gated on `caps_known` the same way:
        # burnt and left read null without a cap, and print an em dash rather
        # than a raw reading the cap cannot scale.
        left_pct = None if (not caps_known or used is None) else 100 - used
        burnt = hero.get("burnt_equiv") if caps_known else None
        left_tok = hero.get("left_equiv") if caps_known else None
        burn = hero.get("burn_equiv_per_hour")

        # `runs_out_at` can land AFTER the reset -- the week holds at this pace.
        # The backend signals exactly that by keeping `early_hours` null, so
        # reading `runs_out_at` alone (as this did) printed a date for a week
        # that never empties. Gate on early_hours, the way the page does.
        if not caps_known:
            runs_out = "—"
        elif hero.get("runs_out_at") and hero.get("early_hours") is not None:
            runs_out = fmt_day_clock(hero.get("runs_out_at"))
        elif hero.get("resets_at"):
            runs_out = "Holds"
        else:
            runs_out = "—"

        cells = [
            ("BURNT", "—" if burnt is None else f"{fmt_tok(burnt)} / {fmt_pct(used)}", INK),
            ("LEFT", fmt_tok(left_tok),
             PX_LVL[px_left_lvl(left_pct)] if left_pct is not None else DIM),
            ("EMPTY AT", runs_out, INK),
            ("RATE", (f"{fmt_tok(burn)}/hr · {fmt_usd(hero.get('burn_usd_per_hour'))}/hr"
                      if burn else "—"), CLAY),
        ]
        # Measured column starts, not four equal thirds: the rate cell needs
        # roughly twice the width of the left cell, so an even split either
        # clipped the rate or wasted half the row.
        starts = (0, 112, 184, 280)
        self._px(pad, 166, bar_w, 2, LINE)
        for (label, value, colour), dx in zip(cells, starts):
            x = pad + dx
            self.canvas.create_text(x, 180, text=label, anchor="w", fill=DIM, font=("Menlo", LABEL_SIZE))
            self.canvas.create_text(x, 196, text=value, anchor="w", fill=colour, font=("Menlo", VALUE_SIZE))

    def _show(self) -> None:
        """Map the window first, strip the titlebar second.

        macOS is the reason for the order. A window created titleless
        (`overrideredirect` before it is ever mapped) is never brought to the
        front by the window server, so the widget runs invisibly -- the process
        is alive, the canvas is drawn, and nothing appears. Mapping it as an
        ordinary window and stripping the frame afterwards gets both: no
        titlebar, and a window that is actually on screen. Harmless elsewhere.
        """
        self.root.update_idletasks()
        self.root.update()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.lift()

    def run(self) -> None:
        self.draw()
        self._show()
        self._poll()
        self._flame_timer = self.root.after(140, self._flame_tick)
        self.root.mainloop()


def _fix_tcl_tk_env() -> None:
    """Point Tcl/Tk at the interpreter's own lib dir, not a build-machine path.

    uv installs python-build-standalone interpreters, and those bake the
    absolute path of the machine that built them into tkinter's compiled-in
    defaults -- something like `/tools/deps/lib/tcl8.6` from a CI box that
    doesn't exist here. `import tkinter` never touches that path, so it
    always succeeds; only `Tk()` looks up `init.tcl` there and fails. The
    interpreter ships its own copies of the Tcl/Tk runtime under
    `sys.base_prefix/lib`, so if the user hasn't already set TCL_LIBRARY /
    TK_LIBRARY (respect that -- they know their own setup), find those
    directories and point the env vars at them before Tk() ever runs.
    """
    base = sys.base_prefix
    if "TCL_LIBRARY" not in os.environ:
        found = sorted(glob.glob(os.path.join(base, "lib", "tcl8.*")))
        if found:
            os.environ["TCL_LIBRARY"] = found[-1]
    if "TK_LIBRARY" not in os.environ:
        found = sorted(glob.glob(os.path.join(base, "lib", "tk8.*")))
        if found:
            os.environ["TK_LIBRARY"] = found[-1]


def run(port: int = 7433, scope: str | None = None) -> int:
    try:
        import tkinter as tk
    except ImportError:
        # tkinter is stdlib but several builds split it into a separate system
        # package, so the message names the fix per platform rather than the
        # module. Nothing else in ccmetrics can hit this path.
        print("the widget needs tkinter, which this Python build is missing.\n"
              "  macOS (Homebrew): brew install python-tk\n"
              "  Debian/Ubuntu:    sudo apt install python3-tk\n"
              "  Fedora:           sudo dnf install python3-tkinter")
        return 1
    _fix_tcl_tk_env()
    try:
        Widget(tk, port, scope).run()
    except tk.TclError as e:
        print(f"no display for the widget: {e}")
        return 1
    return 0
