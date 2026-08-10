"""The restart '↻' button -- its hit-test, `_grab`'s drag-guard, and
`Widget._restart_dash`/`_restart_worker`'s pid-file-driven restart. Same
no-Tk `object.__new__(widget.Widget)` pattern as test_widget_spawn.py: none
of this touches the canvas or a real socket/process.
"""

from __future__ import annotations

from types import SimpleNamespace

from ccmetrics import widget


def _bare(port=7433, **extra):
    w = object.__new__(widget.Widget)
    w.port = port
    w._closing = extra.pop("_closing", False)
    w._dash_proc = extra.pop("_dash_proc", None)
    w._dash_spawn_at = extra.pop("_dash_spawn_at", None)
    w._dash_fails = extra.pop("_dash_fails", 0)
    w._restarting = extra.pop("_restarting", False)
    w.data = extra.pop("data", None)
    w.error = extra.pop("error", None)
    for k, v in extra.items():
        setattr(w, k, v)
    return w


# --- hit-test / drag guard ---------------------------------------------------


def test_hit_restart_rejects_points_outside_the_button():
    x0, y0, x1, y1 = widget._restart_rect()
    assert not widget._hit_restart(x0 - 5, (y0 + y1) / 2)
    assert not widget._hit_restart(x1 + 5, (y0 + y1) / 2)
    assert not widget._hit_restart((x0 + x1) / 2, y0 - 5)


def test_hit_restart_accepts_a_point_inside_the_button():
    x0, y0, x1, y1 = widget._restart_rect()
    assert widget._hit_restart((x0 + x1) / 2, (y0 + y1) / 2)


def test_restart_rect_sits_left_of_min_with_the_same_gap_min_uses_from_close():
    mx0, _my0, mx1, _my1 = widget._min_rect()
    cx0, _cy0, _cx1, _cy1 = widget._close_rect()
    rx0, _ry0, rx1, _ry1 = widget._restart_rect()
    assert rx1 == mx0 - (cx0 - mx1)
    assert rx1 - rx0 == mx1 - mx0


def test_grab_ignores_a_press_on_the_restart_button():
    w = _bare()
    x0, y0, x1, y1 = widget._restart_rect()
    e = SimpleNamespace(x=(x0 + x1) / 2, y=(y0 + y1) / 2)
    widget.Widget._grab(w, e)
    assert w._drag is None


def test_grab_still_starts_a_drag_off_every_button():
    w = _bare()
    e = SimpleNamespace(x=widget.WIDTH / 2, y=widget.HEIGHT / 2)
    widget.Widget._grab(w, e)
    assert w._drag == (e.x, e.y)


# --- _restart_dash: the closing guard ---------------------------------------


def test_restart_dash_does_nothing_while_closing(monkeypatch):
    started = []
    monkeypatch.setattr(widget.threading, "Thread", lambda *a, **k: started.append(1))
    w = _bare(_closing=True, data="sentinel", error="sentinel-err")
    widget.Widget._restart_dash(w)
    assert started == []
    assert w.data == "sentinel"
    assert w.error == "sentinel-err"
    assert w._restarting is False


# --- _restart_worker: the pid-file-driven restart itself --------------------


def test_restart_worker_resets_cooldown_and_strikes_on_a_valid_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: (111, 7433))
    monkeypatch.setattr(widget, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(widget, "_dash_answers", lambda port, timeout=1.5: True)
    monkeypatch.setattr(widget, "_wait_port_silent", lambda port: calls.append("wait"))
    monkeypatch.setattr(widget.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(widget.Widget, "_maybe_spawn_dash", lambda self: calls.append("spawn"))
    monkeypatch.setattr(widget.Widget, "_fetch", lambda self: calls.append("fetch"))

    w = _bare(_dash_spawn_at=123.0, _dash_fails=2, _restarting=True)
    widget.Widget._restart_worker(w)

    assert ("kill", 111, widget.signal.SIGTERM) in calls
    assert "wait" in calls
    # the kill/wait happen before the D54 state is cleared and a fresh spawn
    # + refetch are attempted
    assert calls.index("wait") < calls.index("spawn") < calls.index("fetch")
    assert w._dash_spawn_at is None  # D54's cooldown reset...
    assert w._dash_fails == 0  # ...and its 2-strike cap, both cleared
    assert w._restarting is False  # _poll_restart's cue that this is done


def test_restart_worker_with_no_pid_file_only_refetches(monkeypatch):
    kill_calls, spawn_calls, fetch_calls = [], [], []
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: None)
    monkeypatch.setattr(widget.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr(widget.Widget, "_maybe_spawn_dash", lambda self: spawn_calls.append(1))
    monkeypatch.setattr(widget.Widget, "_fetch", lambda self: fetch_calls.append(1))

    w = _bare(_dash_spawn_at=123.0, _dash_fails=1, _restarting=True)
    widget.Widget._restart_worker(w)

    assert kill_calls == []  # never hunts for a process to kill without a pid file
    assert spawn_calls == []  # no restart attempted -- this is a plain refresh
    assert fetch_calls == [1]
    assert w.error == "no pid file — refreshing :7433…"
    # a fallback refresh must not touch the D54 cooldown/strike state --
    # no restart was actually attempted
    assert w._dash_spawn_at == 123.0
    assert w._dash_fails == 1
    assert w._restarting is False


def test_restart_worker_leaves_a_live_pid_alone_when_its_port_does_not_answer(monkeypatch):
    # Alive is not enough on its own: the recorded port has to answer as a
    # ccmetrics dash too, or the pid file is naming something else entirely.
    calls = []
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: (222, 9999))
    monkeypatch.setattr(widget, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(widget, "_dash_answers", lambda port, timeout=1.5: False)
    monkeypatch.setattr(widget.os, "kill", lambda pid, sig: calls.append("kill"))
    monkeypatch.setattr(widget, "_wait_port_silent", lambda port: calls.append("wait"))
    monkeypatch.setattr(widget.Widget, "_maybe_spawn_dash", lambda self: calls.append("spawn"))
    monkeypatch.setattr(widget.Widget, "_fetch", lambda self: calls.append("fetch"))

    w = _bare(_restarting=True)
    widget.Widget._restart_worker(w)

    assert "kill" not in calls
    assert "wait" not in calls
    assert w._restarting is False


def test_restart_worker_leaves_a_dead_pid_alone(monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: (333, 7433))
    monkeypatch.setattr(widget, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(
        widget, "_dash_answers", lambda port, timeout=1.5: (_ for _ in ()).throw(
            AssertionError("must not probe a port for a pid that's already dead")
        )
    )
    monkeypatch.setattr(widget.os, "kill", lambda pid, sig: calls.append("kill"))
    monkeypatch.setattr(widget.Widget, "_maybe_spawn_dash", lambda self: None)
    monkeypatch.setattr(widget.Widget, "_fetch", lambda self: None)

    w = _bare(_restarting=True)
    widget.Widget._restart_worker(w)

    assert calls == []
