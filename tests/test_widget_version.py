"""D60: the widget notices when the dash it is polling on `/api/windows`
reports a different `server_version` than the widget's own `__version__`,
and restarts it -- reusing the D59 pid-file restart (`_restart_worker`)
`_restart_dash`'s ↻ button already drives, never a second mechanism.

`_check_version` is what `_fetch` calls on a successful fetch; it never
touches the canvas (see its own docstring), so the same no-Tk
`object.__new__(widget.Widget)` pattern as test_widget_restart.py /
test_widget_spawn.py covers it without a real Tk root.
"""

from __future__ import annotations

from ccmetrics import widget


class _FakeThread:
    """Stands in for `threading.Thread(target=..., daemon=True)`: records
    the callable it was given and never actually runs it -- these tests
    check that a restart was *scheduled*, not that `_restart_worker` itself
    (covered by test_widget_restart.py) ran.
    """

    def __init__(self, target=None, daemon=None):
        self.target = target
        self.daemon = daemon

    def start(self) -> None:
        pass


def _bare(**extra):
    w = object.__new__(widget.Widget)
    w.data = extra.pop("data", None)
    w._restarting = extra.pop("_restarting", False)
    w._version_restarted = extra.pop("_version_restarted", False)
    w._version_notice = extra.pop("_version_notice", None)
    for k, v in extra.items():
        setattr(w, k, v)
    return w


def test_matching_version_does_not_restart(monkeypatch):
    started = []
    monkeypatch.setattr(widget.threading, "Thread", lambda *a, **k: started.append(1))
    w = _bare(data={"server_version": widget.__version__})
    widget.Widget._check_version(w)
    assert started == []
    assert w._restarting is False
    assert w._version_restarted is False
    assert w._version_notice is None


def test_mismatched_version_restarts_exactly_once(monkeypatch):
    started = []
    monkeypatch.setattr(widget.threading, "Thread",
                         lambda target=None, daemon=None: started.append(target) or _FakeThread())
    w = _bare(data={"server_version": "0.1.0"})
    widget.Widget._check_version(w)
    assert started == [w._restart_worker]
    assert w._restarting is True
    assert w._version_restarted is True
    assert w._version_notice is None


def test_still_mismatched_after_one_restart_does_not_restart_again(monkeypatch):
    started = []
    monkeypatch.setattr(widget.threading, "Thread", lambda *a, **k: started.append(1))
    w = _bare(data={"server_version": "0.1.0"}, _version_restarted=True)
    widget.Widget._check_version(w)
    assert started == []  # no second restart
    assert w._restarting is False
    assert w._version_notice == f"dash is v0.1.0, this is v{widget.__version__}"


def test_missing_server_version_is_treated_as_outdated_and_restarted_once(monkeypatch):
    started = []
    monkeypatch.setattr(widget.threading, "Thread",
                         lambda target=None, daemon=None: started.append(target) or _FakeThread())
    w = _bare(data={})  # a dash too old to report server_version at all
    widget.Widget._check_version(w)
    assert len(started) == 1
    assert w._version_restarted is True
    assert w._restarting is True


def test_check_version_does_nothing_while_a_restart_is_already_in_flight(monkeypatch):
    started = []
    monkeypatch.setattr(widget.threading, "Thread", lambda *a, **k: started.append(1))
    w = _bare(data={"server_version": "0.1.0"}, _restarting=True)
    widget.Widget._check_version(w)
    assert started == []
    assert w._version_restarted is False


def test_check_version_does_nothing_with_no_data_yet():
    w = _bare(data=None)
    widget.Widget._check_version(w)  # must not raise
    assert w._version_restarted is False


def test_a_fresh_match_after_restart_clears_a_stale_notice(monkeypatch):
    # A prior mismatch left a notice; a later fetch that finally agrees
    # must clear it rather than leave a stale warning on screen forever.
    started = []
    monkeypatch.setattr(widget.threading, "Thread", lambda *a, **k: started.append(1))
    w = _bare(data={"server_version": widget.__version__}, _version_restarted=True,
              _version_notice="dash is v0.1.0, this is v0.2.0")
    widget.Widget._check_version(w)
    assert started == []
    assert w._version_notice is None


# --- restart_if_outdated: the plain `ccmetrics` summary's own check --------


def test_restart_if_outdated_does_nothing_on_a_matching_version(monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: calls.append("pid") or (1, 7433))
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                         lambda *a, **k: _FakeResponse({"server_version": widget.__version__}))
    monkeypatch.setattr(widget.subprocess, "Popen", lambda *a, **k: calls.append("spawn"))
    widget.restart_if_outdated()
    assert calls == []  # no pid lookup, no restart -- versions already agree


def test_restart_if_outdated_kills_and_respawns_on_a_mismatch(monkeypatch):
    calls = []
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                         lambda *a, **k: _FakeResponse({"server_version": "0.1.0"}))
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: (111, 7433))
    monkeypatch.setattr(widget, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(widget.os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))
    monkeypatch.setattr(widget, "_wait_port_silent", lambda port: calls.append("wait"))
    monkeypatch.setattr(widget.subprocess, "Popen", lambda *a, **k: calls.append("spawn"))
    widget.restart_if_outdated(port=7433)
    assert ("kill", 111, widget.signal.SIGTERM) in calls
    assert "wait" in calls
    assert "spawn" in calls


def test_restart_if_outdated_swallows_a_failed_fetch_and_never_raises(monkeypatch):
    def _raise(*a, **k):
        raise widget.urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(widget.urllib.request, "urlopen", _raise)
    widget.restart_if_outdated()  # must not raise -- nothing is listening


def test_restart_if_outdated_swallows_a_failed_kill_and_never_raises(monkeypatch):
    monkeypatch.setattr(widget.urllib.request, "urlopen",
                         lambda *a, **k: _FakeResponse({"server_version": "0.1.0"}))
    monkeypatch.setattr(widget, "_read_pid_file", lambda path: (111, 7433))
    monkeypatch.setattr(widget, "_pid_alive", lambda pid: True)

    def _raise_kill(pid, sig):
        raise OSError("boom")

    monkeypatch.setattr(widget.os, "kill", _raise_kill)
    widget.restart_if_outdated()  # must not raise


class _FakeResponse:
    def __init__(self, body: dict):
        import json

        self._body = json.dumps(body).encode()
        self.status = 200

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
