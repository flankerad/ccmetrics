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
"""

from __future__ import annotations

import glob
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

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

# The flame, one frame of the page's three, on a 7x10 grid. The page animates
# its tip; a widget that redraws every 20 seconds does not, so it keeps the
# frame with the tip centred and drops the flicker.
FLAME_C = {
    "R": "#c9341f", "O": "#e0512a", "Y": "#f07f2c",
    "W": "#ffe6b0", "C": "#ffc65e", "D": "#a82a18",
}
FLAME = [
    "...R...", "...R...", "..OOO..", "..OOO..", ".YYWYY.",
    ".YYWYY.", ".YYWYY.", "OOCCCOO", "OOCCCOO", ".DDDDD.",
]

CELLS = 40
POLL_SECONDS = 20
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
# The collapsed panel's height: `draw`'s sub-line sits at y=38, so this clears
# that row, the sub-line's own text height at SUB_SIZE, and the 2px bottom
# border -- face, headline and sub-line stay visible and nothing below them
# does. Not iconify()d: `_show` strips the titlebar with `overrideredirect`,
# and macOS will not reliably restore a borderless minimized window, so
# minimizing here means collapsing the panel in place instead.
MIN_HEIGHT = 38 + SUB_SIZE + 2


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

    def _icon(self):
        """The Dock tile: the same pixel flame the fuse burns, on the panel's
        own dark square. Built at runtime rather than shipped as a file --
        there is no icon asset in the repo (the page's favicon is a 404), and
        adding one would mean a binary in a package that has none.

        `put` takes a row-major list of colour rows, so the flame is scaled by
        repeating each pixel `s` times across and each row `s` times down.
        """
        size, grid = 64, len(FLAME)
        s = 5  # 7x10 flame at 5x -> 35x50 inside a 64px tile
        pad_x = (size - len(FLAME[0]) * s) // 2
        pad_y = (size - grid * s) // 2
        rows = [[SURF] * size for _ in range(size)]
        for r, row in enumerate(FLAME):
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
        if _hit_close(e.x, e.y) or _hit_min(e.x, e.y):
            # The X and the minimize button handle their own clicks; this
            # must not start a drag. Clear the anchor rather than just
            # skipping the update -- a stale one would let a press-on-a-
            # button-then-drag-off jump the window by the delta from
            # whatever the *previous* click left behind.
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
        self.root.destroy()

    # ---- data ----------------------------------------------------------

    def _url(self) -> str:
        url = f"http://127.0.0.1:{self.port}/api/windows"
        return url + f"?project={urllib.parse.quote(self.scope)}" if self.scope else url

    def _fetch(self) -> None:
        """Runs off the main thread -- Tk is not thread-safe, so this only ever
        assigns to `self.data`/`self.error` and lets the `after` timer redraw.

        Checks `_closing` twice: once up front, so a fetch that never even
        starts after `_shutdown` skips the request outright, and again after
        `urlopen` returns, because a fetch already inside that call when the
        X is clicked passed the first check seconds earlier and would still
        write into `self.data`/`self.error` on the way out otherwise.
        Harmless on its own -- these are plain attributes, not Tk -- but
        nothing should be assigning into a widget that is going away.
        """
        if self._closing:
            return
        try:
            with urllib.request.urlopen(self._url(), timeout=4) as r:
                body, err = json.loads(r.read().decode()), None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # Names the fix, not the fault: the widget is useless without the
            # dash serving, and "no dash on :7433" left the reader guessing.
            body, err = None, f"nothing on :{self.port} — run  ccmetrics dash"
        if self._closing:
            return
        if err is None:
            self.data = body
        self.error = err

    def _poll(self) -> None:
        if self._closing:
            return
        threading.Thread(target=self._fetch, daemon=True).start()
        self._draw_timer = self.root.after(400, self.draw)
        self._poll_timer = self.root.after(POLL_SECONDS * 1000, self._poll)

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

    def _flame(self, cx: float, bottom: float) -> None:
        s = 2  # 7x10 grid at 2px -> a 14x20 flame, the page's own footprint
        left = cx - (len(FLAME[0]) * s) / 2
        top = bottom - len(FLAME) * s
        for r, row in enumerate(FLAME):
            for c, ch in enumerate(row):
                if ch != ".":
                    self._px(left + c * s, top + r * s, s, s, FLAME_C[ch])

    def _clock(self, cx: float, top: float, fuse_top: float) -> None:
        """21 columns wide at a 2px pitch, digits on a 4-column pitch. Drawn
        case -> face -> digits, the reverse of the page's shadow order, because
        a canvas paints later shapes on top where box-shadow paints earlier
        ones on top. Same picture, opposite order.
        """
        s = 2
        left = cx - (21 * s) / 2
        self._px(left, top, 21 * s, 11 * s, DIM)
        self._px(left + s, top + s, 19 * s, 9 * s, INK)
        now = datetime.now()
        text = f"{now.hour:02d}{now.minute:02d}"
        for glyph_i, col in enumerate((2, 6, 12, 16)):
            glyph = PX_DIGIT[int(text[glyph_i])]
            for gr in range(5):
                for gc in range(3):
                    if glyph[gr * 3 + gc] == "1":
                        self._px(left + (col + gc) * s, top + (gr + 3) * s, s, s, BG)
        for row in (4, 6):  # the colon, between the two pairs
            self._px(left + 10 * s, top + row * s, s, s, BG)
        # feet, and the stem down to the bar it marks
        for foot in (2, 4, 16, 18):
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

        if self.data is None:
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
        face_y = (head_y + sub_y) / 2
        self.canvas.create_text(16, face_y, text=_face(pct_left), anchor="w",
                                fill=CLAY, font=("Menlo", face_size))
        text_x = 16 + face_size + 8
        self.canvas.create_text(text_x, head_y, text=verdict.upper(), anchor="w", fill=INK,
                                font=("Menlo", HEAD_SIZE))
        self.canvas.create_text(text_x, sub_y, text=sub, anchor="w", fill=DIM, font=("Menlo", SUB_SIZE))

        if self._minimized:
            # Collapsed: face, headline and sub-line are the whole panel --
            # stop here, before the week's fuse and everything under it.
            return

        # No label over the week's bar. One would have to clear the clock,
        # which can sit at any point along the row, and stacking it above the
        # clock put three tight lines over a tall gap. The verdict above names
        # the week already, and only the block bar below needs saying which.
        fuse_top, bar_h, pad = 82, 18, 16
        self._bar(fuse_top, bar_h, used_pct, source == "cap", fuse=True)

        bar_l, bar_w = pad, WIDTH - pad * 2
        if clock_pct is not None:
            self._clock(bar_l + bar_w * clock_pct / 100, 48, fuse_top)
        if source == "cap":
            self._flame(bar_l + bar_w * used_pct / 100, fuse_top + bar_h)

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
