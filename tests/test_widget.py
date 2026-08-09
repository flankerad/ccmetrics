"""`ccmetrics/widget.py`'s pure helpers -- the face ladder and the verdict's
cap/week sub-line. No Tk needed: `widget` only imports it inside methods that
build a window, never at module scope.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import pytest

from ccmetrics import widget


# --- fmt_clock / fmt_day_clock: the API's UTC on this machine's clock ------
# The server sends `...Z`; the page converts it (new Date(iso).getHours()).
# The widget used to strftime the aware datetime straight, so a 5H BLOCK reset
# the page called 18:20 local printed as the UTC 12:50 -- and, for an instant
# late in the UTC day, EMPTY AT named the wrong weekday. Expected strings are
# derived here rather than hard-coded, so the suite holds in any timezone.

UTC_MIDDAY = "2026-08-09T12:50:00Z"
UTC_LATE = "2026-08-09T23:40:00Z"  # already Monday east of UTC


@pytest.fixture
def tz():
    """Pin a timezone for one test, then hand the process clock back.

    Restored by hand rather than via monkeypatch: the env has to go back
    *before* the tzset that re-reads it. `time.tzset` is POSIX-only, so the
    tests that ask for a pinned zone are macOS/Linux-only; the derived-value
    tests beside them carry the same coverage everywhere else.
    """
    before = os.environ.get("TZ")

    def _set(name):
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    if before is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = before
    time.tzset()


def test_fmt_clock_converts_an_aware_utc_stamp_to_local():
    expected = datetime.fromisoformat(UTC_MIDDAY).astimezone().strftime("%H:%M")
    assert widget.fmt_clock(UTC_MIDDAY) == expected


def test_fmt_clock_in_a_pinned_zone_matches_the_page(tz):
    # A known-good worked example: 12:50Z is 18:20 in +05:30, which is exactly
    # the pair the widget and the dash disagreed on.
    tz("Asia/Kolkata")
    assert widget.fmt_clock(UTC_MIDDAY) == "18:20"


def test_fmt_day_clock_converts_an_aware_utc_stamp_to_local():
    expected = datetime.fromisoformat(UTC_MIDDAY).astimezone().strftime("%a %H:%M").upper()
    assert widget.fmt_day_clock(UTC_MIDDAY) == expected


def test_fmt_day_clock_names_the_local_weekday_not_the_utc_one():
    # The case that mattered: an instant late in the UTC day belongs to the
    # next weekday east of UTC, and EMPTY AT printed that weekday.
    expected = datetime.fromisoformat(UTC_LATE).astimezone().strftime("%a %H:%M").upper()
    assert widget.fmt_day_clock(UTC_LATE) == expected


def test_fmt_day_clock_in_a_pinned_zone_rolls_the_weekday_over(tz):
    # 2026-08-09 is a Sunday; 23:40Z is 05:10 Monday in +05:30.
    tz("Asia/Kolkata")
    assert widget.fmt_day_clock(UTC_LATE) == "MON 05:10"


def test_fmt_clock_leaves_a_naive_stamp_alone(tz):
    # No offset means the server already handed us local time -- the fix must
    # not start shifting those. Pinned to a non-UTC zone so a stray
    # astimezone() would visibly move it.
    tz("Asia/Kolkata")
    assert widget.fmt_clock("2026-08-09T12:50:00") == "12:50"
    assert widget.fmt_day_clock("2026-08-09T12:50:00") == "SUN 12:50"


# By name, not by function object: parametrize binds its values at collection
# time, so a list of the functions themselves would stop going through the
# module attribute -- and stop seeing any replacement of it.
@pytest.mark.parametrize("name", ["fmt_clock", "fmt_day_clock"])
@pytest.mark.parametrize("iso", [None, "", "not-a-timestamp"])
def test_fmt_clocks_fall_back_to_a_dash(name, iso):
    assert getattr(widget, name)(iso) == "—"


def test_fmt_clock_does_not_print_the_raw_utc_clock():
    # The bug itself, stated directly: whatever fmt_clock returns, it is not
    # the UTC wall clock unless this machine happens to sit on UTC.
    if datetime.now().astimezone().utcoffset().total_seconds() == 0:
        pytest.skip("machine is on UTC -- local and UTC clocks are the same")
    raw_utc = datetime.fromisoformat(UTC_MIDDAY).astimezone(timezone.utc).strftime("%H:%M")
    assert widget.fmt_clock(UTC_MIDDAY) != raw_utc


# --- _face: same six-stop ladder as index.html's heroFuse -------------------

def test_face_unknown_is_neutral():
    assert widget._face(None) == "😐"

def test_face_relaxed_above_25_left():
    assert widget._face(30.0) == "🙂"

def test_face_boundary_at_25_left_is_three_quarters_burnt():
    assert widget._face(25.0) == "😬"
    assert widget._face(26.0) == "🙂"

def test_face_boundary_at_15_left():
    assert widget._face(15.0) == "😟"
    assert widget._face(15.1) == "😬"

def test_face_boundary_at_10_left():
    assert widget._face(10.0) == "😰"
    assert widget._face(10.1) == "😟"

def test_face_hot_end_at_5_left():
    assert widget._face(5.0) == "🥵"
    assert widget._face(0.0) == "🥵"


# --- _cap_week_sub: replaces the old "N points ahead/behind the clock" line -

def test_cap_week_sub_both_known():
    assert widget._cap_week_sub(25.0, 4.0, "") == "25% of the cap is left, with 4% of the week to go."

def test_cap_week_sub_appends_tail_without_trailing_period():
    out = widget._cap_week_sub(25.0, 4.0, "— plenty of room.")
    assert out == "25% of the cap is left, with 4% of the week to go — plenty of room."

def test_cap_week_sub_drops_unknown_clause_instead_of_a_hole():
    assert widget._cap_week_sub(None, 4.0, "") == "4% of the week to go."
    assert widget._cap_week_sub(25.0, None, "") == "25% of the cap is left."

def test_cap_week_sub_both_unknown_falls_back_to_tail_only():
    assert widget._cap_week_sub(None, None, "— plenty of room.") == "— plenty of room."


# --- _verdict: exhaustion branches are the widget's own ladder -- the one
# index.html's heroFuse (nearlyGone/nearlySpent/gettingTight, ~lines
# 414-416) now follows, not the other way around. 25 is deliberately the
# same edge where FACE_LADDER's 😬 starts.

def test_verdict_edges_are_the_contract():
    assert widget.VERDICT_EDGES == (5, 12, 25)

def test_verdict_boundary_at_5_left_is_almost_gone():
    verdict, sub = widget._verdict({"used_pct": 95.0}, caps_known=True, hook_ran=True)
    assert verdict == "The week is almost gone."
    assert sub == "5% of the cap is left, whatever the pace."

def test_verdict_just_above_5_left_is_nearly_spent():
    verdict, sub = widget._verdict({"used_pct": 94.9}, caps_known=True, hook_ran=True)
    assert verdict == "Nearly spent."
    assert sub == "5% of the cap is left."

def test_verdict_just_below_12_left_is_still_nearly_spent():
    verdict, sub = widget._verdict({"used_pct": 89.0}, caps_known=True, hook_ran=True)
    assert verdict == "Nearly spent."
    assert sub == "11% of the cap is left."

def test_verdict_boundary_at_12_left_is_getting_tight():
    # 12 itself is NOT < 12, so it falls to gettingTight -- the same edge
    # the dash's nearlySpent/gettingTight split draws.
    verdict, sub = widget._verdict({"used_pct": 88.0}, caps_known=True, hook_ran=True)
    assert verdict == "Getting tight."
    assert sub == "12% of the cap is left."

def test_verdict_just_below_25_left_is_still_getting_tight():
    verdict, sub = widget._verdict({"used_pct": 76.0}, caps_known=True, hook_ran=True)
    assert verdict == "Getting tight."
    assert sub == "24% of the cap is left."

def test_verdict_boundary_at_25_left_falls_through_to_pace():
    # 25 itself is NOT < 25, so it falls past gettingTight to the
    # clock-relative branches -- the same edge the dash's gettingTight
    # cutoff draws, and where FACE_LADDER's 😬 begins.
    hero = {"used_pct": 75.0, "clock_pct": 60.0, "behind": 20.0}
    verdict, _ = widget._verdict(hero, caps_known=True, hook_ran=True)
    assert verdict == "Burning faster than it refills."

def test_verdict_23_left_is_getting_tight():
    # The finish criterion for this pass: 23% of the cap left clears the
    # < 25 edge, so it lands on "Getting tight." -- the same read index.html's
    # gettingTight now gives for the same input.
    verdict, sub = widget._verdict({"used_pct": 77.0}, caps_known=True, hook_ran=True)
    assert verdict == "Getting tight."
    assert sub == "23% of the cap is left."

def test_verdict_exhaustion_sub_never_says_of_the_week_is_left():
    for used_pct in (99.0, 92.0, 88.0):
        _, sub = widget._verdict({"used_pct": used_pct}, caps_known=True, hook_ran=True)
        assert "of the week is left" not in sub


def test_verdict_burning_faster_has_no_points_language():
    hero = {"used_pct": 50.0, "clock_pct": 30.0, "behind": 20.0}
    verdict, sub = widget._verdict(hero, caps_known=True, hook_ran=True)
    assert verdict == "Burning faster than it refills."
    assert "points" not in sub
    assert "50% of the cap is left" in sub
    assert "70% of the week to go" in sub

def test_verdict_comfortably_inside_has_no_points_language():
    hero = {"used_pct": 30.0, "clock_pct": 60.0, "behind": -30.0}
    verdict, sub = widget._verdict(hero, caps_known=True, hook_ran=True)
    assert verdict == "Comfortably inside the week."
    assert "points" not in sub
    assert "70% of the cap is left" in sub
    assert "40% of the week to go" in sub


# --- _close_rect / _hit_close: the top-right close X's hit box -------------

def test_close_rect_sits_inside_the_border_top_right_corner():
    x0, y0, x1, y1 = widget._close_rect()
    assert 2 < x0 < x1 < widget.WIDTH - 2
    assert 2 < y0 < y1 < widget.HEIGHT - 2
    # right-of-centre and near the top, not drifted toward the headline/face
    assert x0 > widget.WIDTH / 2
    assert y1 < 40

def test_close_rect_side_matches_head_size():
    x0, y0, x1, y1 = widget._close_rect()
    assert x1 - x0 == widget.HEAD_SIZE
    assert y1 - y0 == widget.HEAD_SIZE

def test_hit_close_true_inside_the_box():
    x0, y0, x1, y1 = widget._close_rect()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    assert widget._hit_close(cx, cy) is True

def test_hit_close_true_on_the_box_edges():
    x0, y0, x1, y1 = widget._close_rect()
    assert widget._hit_close(x0, y0) is True
    assert widget._hit_close(x1, y1) is True

def test_hit_close_false_just_outside_the_box():
    x0, y0, x1, y1 = widget._close_rect()
    assert widget._hit_close(x0 - 1, y0) is False
    assert widget._hit_close(x1 + 1, y1) is False
    assert widget._hit_close(x0, y1 + 1) is False

def test_hit_close_false_far_from_the_corner():
    assert widget._hit_close(16, 18) is False  # the headline/face's own spot
    assert widget._hit_close(0, 0) is False


# --- _grab / _move: a press on the X must not arm the window drag ----------

class _FakeEvent:
    def __init__(self, x, y, x_root=0, y_root=0):
        self.x, self.y, self.x_root, self.y_root = x, y, x_root, y_root


def test_grab_on_the_close_x_clears_the_drag_anchor():
    w = object.__new__(widget.Widget)
    w._drag = (5, 5)  # a stale anchor from an earlier, real drag
    x0, y0, _, _ = widget._close_rect()
    widget.Widget._grab(w, _FakeEvent(x0, y0))
    assert w._drag is None

def test_grab_off_the_close_x_sets_the_drag_anchor():
    w = object.__new__(widget.Widget)
    w._drag = None
    widget.Widget._grab(w, _FakeEvent(16, 18))
    assert w._drag == (16, 18)

def test_move_after_a_close_x_press_does_not_jump_the_window():
    w = object.__new__(widget.Widget)
    w._drag = None  # what _grab leaves behind after a press on the X
    calls = []
    w.root = type("Root", (), {"geometry": lambda self, s: calls.append(s)})()
    widget.Widget._move(w, _FakeEvent(0, 0, x_root=999, y_root=999))
    assert calls == []


# --- _shutdown / the close X: one teardown path, no crash on click ---------
# The X used to destroy the toplevel from inside its own canvas-item click
# dispatch -- Tk freed the canvas mid-dispatch, and macOS reported that as
# "python quit unexpectedly". _shutdown is the single path every close route
# now funnels through; these pin its contract without opening a real window.

class _StubRoot:
    """Records `after_cancel`/`destroy` calls; nothing else."""

    def __init__(self):
        self.cancelled = []
        self.destroyed = False

    def after_cancel(self, id_):
        self.cancelled.append(id_)

    def destroy(self):
        self.destroyed = True


def test_shutdown_cancels_both_timers_then_destroys():
    w = object.__new__(widget.Widget)
    w._closing = False
    w._draw_timer = "draw_id"
    w._poll_timer = "poll_id"
    w._flame_timer = "flame_id"
    w.root = _StubRoot()
    widget.Widget._shutdown(w)
    assert w._closing is True
    assert w.root.cancelled == ["draw_id", "poll_id", "flame_id"]
    assert w.root.destroyed is True


def test_shutdown_tolerates_no_pending_timers():
    w = object.__new__(widget.Widget)
    w._closing = False
    w._draw_timer = None
    w._poll_timer = None
    w._flame_timer = None
    w.root = _StubRoot()
    widget.Widget._shutdown(w)
    assert w.root.cancelled == []
    assert w.root.destroyed is True


def test_shutdown_is_a_noop_once_already_closing():
    # A second close route (e.g. the X's deferred call landing after Escape
    # already fired) must not double-cancel or double-destroy.
    w = object.__new__(widget.Widget)
    w._closing = True
    w.root = _StubRoot()
    widget.Widget._shutdown(w)
    assert w.root.cancelled == []
    assert w.root.destroyed is False


def test_draw_noops_once_closing():
    w = object.__new__(widget.Widget)
    w._closing = True

    class _BoomCanvas:
        def delete(self, *_a):
            raise AssertionError("draw touched a canvas that is closing")

    w.canvas = _BoomCanvas()
    widget.Widget.draw(w)  # must return before it ever reaches the canvas


def test_poll_noops_once_closing():
    w = object.__new__(widget.Widget)
    w._closing = True
    w._draw_timer = "unchanged"
    w._poll_timer = "unchanged"
    # No `after`/`after_cancel` here -- if the guard didn't fire, scheduling
    # the next timers would blow up on this bare object.
    w.root = object()
    widget.Widget._poll(w)
    assert w._draw_timer == "unchanged"
    assert w._poll_timer == "unchanged"


def test_fetch_noops_once_closing():
    w = object.__new__(widget.Widget)
    w._closing = True
    w.data = "before"
    w.error = "before"
    widget.Widget._fetch(w)
    assert w.data == "before"
    assert w.error == "before"


def test_fetch_discards_a_result_that_lands_after_shutdown(monkeypatch):
    # The in-flight case: `_shutdown` runs while `urlopen` is still blocked,
    # not before the thread even starts. The result must be discarded either
    # way, not just when `_closing` was already set at the top of `_fetch`.
    w = object.__new__(widget.Widget)
    w._closing, w.data, w.error, w.port, w.scope = False, None, None, 7433, None

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            w._closing = True  # shutdown lands mid-request
            return b'{"ok": true}'

    monkeypatch.setattr(widget.urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse())
    widget.Widget._fetch(w)
    assert w.data is None
    assert w.error is None


class _StubImage:
    def put(self, *_a, **_k):
        pass


class _StubCanvas:
    def __init__(self, *_a, **_k):
        self.bindings = {}
        self.tag_bindings = {}

    def pack(self):
        pass

    def bind(self, seq, func):
        self.bindings[seq] = func

    def tag_bind(self, tag, seq, func):
        self.tag_bindings[(tag, seq)] = func


class _StubRootFull(_StubRoot):
    def __init__(self):
        super().__init__()
        self.after_idle_calls = []

    def title(self, *_a):
        pass

    def configure(self, **_k):
        pass

    def geometry(self, *_a):
        pass

    def iconphoto(self, *_a):
        pass

    def bind(self, seq, func):
        pass

    def after_idle(self, func):
        self.after_idle_calls.append(func)
        return "idle_id"


class _StubTk:
    def Tk(self):
        return _StubRootFull()

    def Canvas(self, root, **_k):
        return _StubCanvas(root)

    def PhotoImage(self, **_k):
        return _StubImage()


def test_close_x_click_schedules_shutdown_instead_of_destroying_inline():
    w = widget.Widget(_StubTk(), 7433)
    handler = w.canvas.tag_bindings[("close", "<Button-1>")]
    handler(_FakeEvent(0, 0))
    # after_idle, not an inline root.destroy() -- see _shutdown's docstring
    # for why destroying from inside the item's own dispatch crashes.
    assert w.root.after_idle_calls == [w._shutdown]
    assert w.root.destroyed is False
