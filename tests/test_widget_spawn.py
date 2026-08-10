"""`Widget._maybe_spawn_dash` / `Widget._fetch_error_text` / the connection-
refused branch of `Widget._fetch` -- the widget's self-starting dash. No Tk
needed: same `object.__new__(widget.Widget)` pattern `test_widget.py` uses
for `_fetch`, since none of this touches the canvas.
"""

from __future__ import annotations

import subprocess
import sys
import urllib.error

import pytest

from ccmetrics import widget


def _bare(port=7433, **extra):
    w = object.__new__(widget.Widget)
    w.port = port
    w._dash_proc = extra.pop("_dash_proc", None)
    w._dash_spawn_at = extra.pop("_dash_spawn_at", None)
    w._dash_fails = extra.pop("_dash_fails", 0)
    for k, v in extra.items():
        setattr(w, k, v)
    return w


class _FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code

    def poll(self):
        return self._exit_code


def test_maybe_spawn_dash_spawns_once_when_nothing_tried_yet(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: calls.append((a, k)) or _FakeProc()
    )
    w = _bare(port=7433)
    widget.Widget._maybe_spawn_dash(w)
    assert len(calls) == 1
    (argv,), kwargs = calls[0]
    assert argv == [sys.executable, "-m", "ccmetrics", "dash", "--no-open", "--port", "7433"]
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert w._dash_proc is not None
    assert w._dash_spawn_at is not None
    assert w._dash_fails == 0  # the first attempt itself is not a failure


def test_maybe_spawn_dash_skips_a_second_spawn_while_the_first_is_alive(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(1))
    w = _bare(_dash_proc=_FakeProc(exit_code=None), _dash_spawn_at=0.0)
    widget.Widget._maybe_spawn_dash(w)
    assert calls == []
    assert w._dash_fails == 0


def test_maybe_spawn_dash_holds_off_within_cooldown_after_a_dead_spawn(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(1))
    w = _bare(_dash_proc=_FakeProc(exit_code=1), _dash_spawn_at=widget.time.monotonic())
    widget.Widget._maybe_spawn_dash(w)
    assert calls == []
    # A dead process the cooldown blocked from a respawn still counts as a
    # strike -- otherwise `_dash_fails` would never see repeated failures
    # that are each caught mid-cooldown.
    assert w._dash_fails == 1


def test_maybe_spawn_dash_spawns_again_once_the_cooldown_has_passed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: calls.append(1) or _FakeProc()
    )
    stale = widget.time.monotonic() - widget.DASH_SPAWN_COOLDOWN - 1
    w = _bare(_dash_proc=_FakeProc(exit_code=1), _dash_spawn_at=stale)
    widget.Widget._maybe_spawn_dash(w)
    assert calls == [1]
    assert w._dash_fails == 1


def test_maybe_spawn_dash_gives_up_after_max_consecutive_failures(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(1))
    stale = widget.time.monotonic() - widget.DASH_SPAWN_COOLDOWN - 1
    w = _bare(
        _dash_proc=_FakeProc(exit_code=1),
        _dash_spawn_at=stale,
        _dash_fails=widget.DASH_SPAWN_ATTEMPTS,
    )
    widget.Widget._maybe_spawn_dash(w)
    # Past the cap, a dash that keeps dying must stop getting relaunched --
    # this is the regression test for the endless-respawn loop.
    assert calls == []


def test_fetch_error_text_reads_starting_while_a_spawn_is_pending():
    w = _bare(_dash_spawn_at=widget.time.monotonic(), _dash_fails=0)
    assert widget.Widget._fetch_error_text(w) == "starting dash on :7433…"


def test_fetch_error_text_falls_back_once_the_cooldown_has_passed():
    stale = widget.time.monotonic() - widget.DASH_SPAWN_COOLDOWN - 1
    w = _bare(_dash_spawn_at=stale, _dash_fails=0)
    assert widget.Widget._fetch_error_text(w) == "nothing on :7433 — run  ccmetrics dash"


def test_fetch_error_text_names_the_manual_fix_once_attempts_are_used_up():
    # Even with a spawn "recent" by the clock, exhausted attempts must win --
    # this is what makes the manual-fix text reachable at all once
    # `_maybe_spawn_dash` has given up.
    w = _bare(_dash_spawn_at=widget.time.monotonic(), _dash_fails=widget.DASH_SPAWN_ATTEMPTS)
    assert widget.Widget._fetch_error_text(w) == "nothing on :7433 — run  ccmetrics dash"


def test_fetch_error_text_with_no_spawn_ever_attempted():
    w = _bare()
    assert widget.Widget._fetch_error_text(w) == "nothing on :7433 — run  ccmetrics dash"


# --- the connection-refused branch of _fetch itself -------------------------


def test_fetch_spawns_a_dash_on_connection_refused(monkeypatch):
    calls = []
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: calls.append(1) or _FakeProc()
    )

    def _raise(*_a, **_k):
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(widget.urllib.request, "urlopen", _raise)
    w = _bare(scope=None)
    w._closing = False
    w.data = None
    w.error = None
    widget.Widget._fetch(w)
    assert calls == [1]
    assert w.error == "starting dash on :7433…"


def test_fetch_does_not_spawn_on_a_timeout_of_a_live_server(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(1))

    def _raise(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(widget.urllib.request, "urlopen", _raise)
    w = _bare(scope=None)
    w._closing = False
    w.data = None
    w.error = None
    widget.Widget._fetch(w)
    assert calls == []
    assert w.error == "nothing on :7433 — run  ccmetrics dash"


def test_fetch_resets_dash_fails_on_a_successful_read(monkeypatch):
    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def read(self):
            return b'{"ok": true}'

    monkeypatch.setattr(widget.urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse())
    w = _bare(scope=None, _dash_fails=widget.DASH_SPAWN_ATTEMPTS)
    w._closing = False
    w.data = None
    w.error = "starting dash on :7433…"
    widget.Widget._fetch(w)
    assert w.data == {"ok": True}
    assert w.error is None
    assert w._dash_fails == 0
