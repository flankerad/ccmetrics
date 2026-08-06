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

import json
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
WIDTH = 440
HEIGHT = 198


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


def fmt_clock(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "—"


def fmt_day_clock(iso: str | None) -> str:
    """`TUE 15:41`, the page's own format for a moment days out.

    The time alone was a lie by omission here: the week runs out three or four
    days ahead, and a bare `15:41` under EMPTY AT reads as tonight. The page
    has always carried the weekday (index.html's DAY_UP); this now matches it.
    """
    if not iso:
        return "—"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return "—"
    return d.strftime("%a %H:%M").upper()


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
    behind = hero.get("behind")
    if left is not None and left <= 3:
        return ("The week is almost gone.", f"{left:.0f}% left, whatever the pace.")
    if left is not None and left < 10:
        return ("Single digits left.", f"{left:.0f}% of the week is left.")
    if left is not None and left < 25:
        return ("Getting tight.", f"{left:.0f}% of the week is left.")
    if behind is not None and behind > 6:
        return ("Burning faster than it refills.", f"{round(behind)} points ahead of the clock.")
    if behind is not None and behind > 0:
        return ("A little ahead of the clock.", f"{behind:.0f} points ahead — worth a glance.")
    if behind is not None:
        return ("Comfortably inside the week.",
                f"{abs(round(behind))} points behind the clock.")
    return ("Tracking the week.", "No reset time yet, so no clock to compare.")


class Widget:
    """The window itself. Owns the canvas and the poll timer, nothing else."""

    def __init__(self, tk, port: int, scope: str | None = None) -> None:
        self.tk = tk
        self.port = port
        self.scope = scope
        self.data: dict | None = None
        self.error: str | None = None

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
        root.bind("<Escape>", lambda _e: root.destroy())
        root.bind("<Double-Button-1>", lambda _e: root.destroy())

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
        self._drag = (e.x, e.y)

    def _move(self, e) -> None:
        self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    # ---- data ----------------------------------------------------------

    def _url(self) -> str:
        url = f"http://127.0.0.1:{self.port}/api/windows"
        return url + f"?project={urllib.parse.quote(self.scope)}" if self.scope else url

    def _fetch(self) -> None:
        """Runs off the main thread -- Tk is not thread-safe, so this only ever
        assigns to `self.data`/`self.error` and lets the `after` timer redraw.
        """
        try:
            with urllib.request.urlopen(self._url(), timeout=4) as r:
                self.data = json.loads(r.read().decode())
                self.error = None
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # Names the fix, not the fault: the widget is useless without the
            # dash serving, and "no dash on :7433" left the reader guessing.
            self.error = f"nothing on :{self.port} — run  ccmetrics dash"

    def _poll(self) -> None:
        threading.Thread(target=self._fetch, daemon=True).start()
        self.root.after(400, self.draw)
        self.root.after(POLL_SECONDS * 1000, self._poll)

    # ---- drawing -------------------------------------------------------

    def _px(self, x: float, y: float, w: float, h: float, fill: str) -> None:
        self.canvas.create_rectangle(x, y, x + w, y + h, fill=fill, outline="")

    def _flame(self, cx: float, bottom: float) -> None:
        s = 2  # 7x10 grid at 2px -> a 14x20 flame, the page's own footprint
        left = cx - (len(FLAME[0]) * s) / 2
        top = bottom - len(FLAME) * s
        for r, row in enumerate(FLAME):
            for c, ch in enumerate(row):
                if ch != ".":
                    self._px(left + c * s, top + r * s, s, s, FLAME_C[ch])

    def _clock(self, cx: float, top: float, fuse_top: float) -> None:
        """19 columns wide at a 2px pitch, digits on a 4-column pitch. Drawn
        case -> face -> digits, the reverse of the page's shadow order, because
        a canvas paints later shapes on top where box-shadow paints earlier
        ones on top. Same picture, opposite order.
        """
        s = 2
        left = cx - (19 * s) / 2
        self._px(left, top, 19 * s, 10 * s, DIM)
        self._px(left + s, top + s, 17 * s, 8 * s, INK)
        now = datetime.now()
        text = f"{now.hour:02d}{now.minute:02d}"
        for glyph_i, col in enumerate((1, 5, 11, 15)):
            glyph = PX_DIGIT[int(text[glyph_i])]
            for gr in range(5):
                for gc in range(3):
                    if glyph[gr * 3 + gc] == "1":
                        self._px(left + (col + gc) * s, top + (gr + 2) * s, s, s, BG)
        for row in (3, 5):  # the colon, between the two pairs
            self._px(left + 9 * s, top + row * s, s, s, BG)
        # feet, and the stem down to the bar it marks
        for foot in (1, 3, 15, 17):
            self._px(left + foot * s, top + 10 * s, s, s, DIM)
        self._px(cx - 1, top + 11 * s, 2, fuse_top - (top + 11 * s), INK)

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
                                    fill=label_c, font=("Menlo", 8))
            self.canvas.create_text(WIDTH - 16, top, text="—", anchor="e",
                                    fill=label_c, font=("Menlo", 8))
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

        self.canvas.create_text(16, top, text=head, anchor="w", fill=label_c, font=("Menlo", 8))
        self.canvas.create_text(WIDTH - 16, top, text=tail, anchor="e",
                                fill=value_c if known else label_c, font=("Menlo", 8))
        self._bar(top + 12, 12, pct if known else 0, known,
                  ghost_to=lands if show_lands else None)
        if show_lands:
            pad = 16
            x = pad + (WIDTH - pad * 2) * min(lands, 100) / 100
            self._px(x - 1, top + 9, 2, 18, CLAY)

    def draw(self) -> None:
        self.canvas.delete("all")
        self._px(0, 0, WIDTH, HEIGHT, SURF)
        for x, y, w, h in ((0, 0, WIDTH, 2), (0, HEIGHT - 2, WIDTH, 2),
                           (0, 0, 2, HEIGHT), (WIDTH - 2, 0, 2, HEIGHT)):
            self._px(x, y, w, h, LINE)

        if self.data is None:
            self.canvas.create_text(
                WIDTH / 2, HEIGHT / 2, text=self.error or "reading the week…",
                fill=DIM, font=("Menlo", 11),
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
        self.canvas.create_text(16, 18, text=verdict.upper(), anchor="w", fill=INK,
                                font=("Menlo", 11))
        self.canvas.create_text(16, 34, text=sub, anchor="w", fill=DIM, font=("Menlo", 9))

        # No label over the week's bar. One would have to clear the clock,
        # which can sit at any point along the row, and stacking it above the
        # clock put three tight lines over a tall gap. The verdict above names
        # the week already, and only the block bar below needs saying which.
        fuse_top, bar_h, pad = 78, 18, 16
        self._bar(fuse_top, bar_h, used_pct, source == "cap", fuse=True)

        bar_l, bar_w = pad, WIDTH - pad * 2
        if clock_pct is not None:
            self._clock(bar_l + bar_w * clock_pct / 100, 48, fuse_top)
        if source == "cap":
            self._flame(bar_l + bar_w * used_pct / 100, fuse_top + bar_h)

        self._px(pad, 112, bar_w, 2, LINE)
        self._block(122, w.get("current_block"), caps_known)

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
        starts = (0, 104, 170, 258)
        self._px(pad, 160, bar_w, 2, LINE)
        for (label, value, colour), dx in zip(cells, starts):
            x = pad + dx
            self.canvas.create_text(x, 172, text=label, anchor="w", fill=DIM, font=("Menlo", 8))
            self.canvas.create_text(x, 186, text=value, anchor="w", fill=colour, font=("Menlo", 10))

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
    try:
        Widget(tk, port, scope).run()
    except tk.TclError as e:
        print(f"no display for the widget: {e}")
        return 1
    return 0
