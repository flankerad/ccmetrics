"""`serve()`'s handling of a port that's already bound (D-port-busy).

Every test here stubs the bind, the reuse probe, the `lsof` lookup, and the
kill -- none ever opens a real socket to a real port or sends a real signal
to a real process. `test_dash_render.py` / `test_dash_api.py` already cover
the request-handling side against a real (loopback, high, throwaway) port;
this file is only about what `serve()` does before it gets that far.
"""

from __future__ import annotations

import errno
import json

import pytest

from ccmetrics.dash import server as dash_server


class _FakeHTTPD:
    """Stands in for ThreadingHTTPServer once bind succeeds. serve_forever
    raises KeyboardInterrupt immediately so no test blocks on a real loop."""

    def __init__(self):
        self.daemon_threads = None
        self.closed = False

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def _bind_ok(_addr, _handler):
    return _FakeHTTPD()


def _addrinuse_error() -> OSError:
    exc = OSError()
    exc.errno = errno.EADDRINUSE
    return exc


def _bind_addrinuse(_addr, _handler):
    raise _addrinuse_error()


def _bind_other_error(_addr, _handler):
    exc = OSError()
    exc.errno = errno.EACCES
    raise exc


@pytest.fixture
def no_sleep(monkeypatch):
    """`_kill_and_wait` polls with real `time.sleep` between checks; tests
    that exercise it stub sleep so they never actually wait."""
    monkeypatch.setattr(dash_server.time, "sleep", lambda _s: None)


# --- serve(): free port ------------------------------------------------------


def test_free_port_serves_normally(monkeypatch, capsys):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_ok)
    opened = []
    monkeypatch.setattr(dash_server.webbrowser, "open", lambda u: opened.append(u))
    rc = dash_server.serve(port=9991, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ccmetrics dash: http://127.0.0.1:9991/  (ctrl-c to stop)" in out
    assert opened == ["http://127.0.0.1:9991/"]


# --- serve(): busy port, another ccmetrics dash is already there ------------


def test_busy_port_answering_as_ccmetrics_is_reused(monkeypatch, capsys):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_addrinuse)
    monkeypatch.setattr(dash_server, "_probe_ccmetrics", lambda port, timeout=0.5: True)

    def _must_not_kill(*_a, **_k):
        raise AssertionError("reuse path must never look for a pid to kill")

    monkeypatch.setattr(dash_server, "_listening_pid", _must_not_kill)
    monkeypatch.setattr(dash_server, "_kill_and_wait", _must_not_kill)
    opened = []
    monkeypatch.setattr(dash_server.webbrowser, "open", lambda u: opened.append(u))

    rc = dash_server.serve(port=9992, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "ccmetrics dash already running: http://127.0.0.1:9992/" in out
    assert opened == ["http://127.0.0.1:9992/"]


# --- serve(): busy port, a foreign process holds it --------------------------


def test_busy_port_foreign_process_gets_killed_then_bound(monkeypatch, capsys):
    calls = {"n": 0}

    def _bind(_addr, _handler):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _addrinuse_error()
        return _FakeHTTPD()

    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind)
    monkeypatch.setattr(dash_server, "_probe_ccmetrics", lambda port, timeout=0.5: False)
    monkeypatch.setattr(dash_server.sys, "platform", "darwin")
    monkeypatch.setattr(dash_server, "_listening_pid", lambda port: ("4321", "python3"))
    kill_calls = []

    def _kill(pid, command, port):
        kill_calls.append((pid, command, port))
        return True

    monkeypatch.setattr(dash_server, "_kill_and_wait", _kill)
    opened = []
    monkeypatch.setattr(dash_server.webbrowser, "open", lambda u: opened.append(u))

    rc = dash_server.serve(port=9993, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 0
    assert kill_calls == [("4321", "python3", 9993)]
    assert calls["n"] == 2  # bind failed once, succeeded on retry after the kill
    assert "ccmetrics dash: http://127.0.0.1:9993/  (ctrl-c to stop)" in out
    assert opened == ["http://127.0.0.1:9993/"]


def test_windows_skips_kill_and_reports_conflict(monkeypatch, capsys):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_addrinuse)
    monkeypatch.setattr(dash_server, "_probe_ccmetrics", lambda port, timeout=0.5: False)
    monkeypatch.setattr(dash_server.sys, "platform", "win32")

    def _must_not_look(*_a, **_k):
        raise AssertionError("must never hunt for a pid to kill on Windows")

    monkeypatch.setattr(dash_server, "_listening_pid", _must_not_look)

    rc = dash_server.serve(port=9994, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "9994" in out
    assert "Windows" in out


def test_busy_port_no_identifiable_process_reports_conflict(monkeypatch, capsys):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_addrinuse)
    monkeypatch.setattr(dash_server, "_probe_ccmetrics", lambda port, timeout=0.5: False)
    monkeypatch.setattr(dash_server.sys, "platform", "darwin")
    monkeypatch.setattr(dash_server, "_listening_pid", lambda port: None)

    rc = dash_server.serve(port=9995, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "9995" in out
    assert "already in use" in out


def test_still_held_after_kill_reports_conflict(monkeypatch, capsys):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_addrinuse)
    monkeypatch.setattr(dash_server, "_probe_ccmetrics", lambda port, timeout=0.5: False)
    monkeypatch.setattr(dash_server.sys, "platform", "darwin")
    monkeypatch.setattr(dash_server, "_listening_pid", lambda port: ("4321", "python3"))
    monkeypatch.setattr(dash_server, "_kill_and_wait", lambda pid, command, port: False)

    rc = dash_server.serve(port=9996, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "still held" in out
    assert "4321" in out


def test_non_addrinuse_oserror_returns_1_no_traceback(monkeypatch, capsys):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_other_error)

    rc = dash_server.serve(port=9997, open_browser=True, reingest_period=None)
    out = capsys.readouterr().out
    assert rc == 1
    assert "9997" in out


# --- _probe_ccmetrics --------------------------------------------------------


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


def test_probe_ccmetrics_true_on_matching_shape(monkeypatch):
    body = json.dumps({"scope": "global", "caps_known": True}).encode()
    monkeypatch.setattr(
        dash_server.urllib.request, "urlopen", lambda url, timeout=0.5: _FakeResp(200, body)
    )
    assert dash_server._probe_ccmetrics(9998) is True


def test_probe_ccmetrics_false_on_wrong_shape(monkeypatch):
    body = json.dumps({"unrelated": True}).encode()
    monkeypatch.setattr(
        dash_server.urllib.request, "urlopen", lambda url, timeout=0.5: _FakeResp(200, body)
    )
    assert dash_server._probe_ccmetrics(9998) is False


def test_probe_ccmetrics_false_on_connection_error(monkeypatch):
    def _raise(url, timeout=0.5):
        raise OSError("connection refused")

    monkeypatch.setattr(dash_server.urllib.request, "urlopen", _raise)
    assert dash_server._probe_ccmetrics(9998) is False


def test_probe_ccmetrics_retries_once_before_giving_up(monkeypatch):
    """A live-but-momentarily-slow dash (one timed-out attempt) must not be
    misread as foreign -- that misread is what would get it killed."""
    body = json.dumps({"scope": "global", "caps_known": True}).encode()
    calls = {"n": 0}

    def _urlopen(url, timeout=0.5):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("slow")
        return _FakeResp(200, body)

    monkeypatch.setattr(dash_server.urllib.request, "urlopen", _urlopen)
    assert dash_server._probe_ccmetrics(9998) is True
    assert calls["n"] == 2


# --- _listening_pid -----------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout


def test_listening_pid_parses_lsof_output(monkeypatch):
    stdout = (
        "COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "python3  4321  user   6u  IPv4 0x0      0t0      TCP  *:9999 (LISTEN)\n"
    )
    monkeypatch.setattr(dash_server.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout))
    assert dash_server._listening_pid(9999) == ("4321", "python3")


def test_listening_pid_none_when_lsof_missing(monkeypatch):
    def _raise(*_a, **_k):
        raise FileNotFoundError("no lsof on this box")

    monkeypatch.setattr(dash_server.subprocess, "run", _raise)
    assert dash_server._listening_pid(9999) is None


def test_listening_pid_none_when_no_listener_found(monkeypatch):
    stdout = "COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
    monkeypatch.setattr(dash_server.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout))
    assert dash_server._listening_pid(9999) is None


def test_listening_pid_none_when_output_names_more_than_one_pid(monkeypatch):
    # Never guess which of two distinct pids to kill.
    stdout = (
        "COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "python3  4321  user   6u  IPv4 0x0      0t0      TCP  *:9999 (LISTEN)\n"
        "node     8765  user   9u  IPv4 0x1      0t0      TCP  *:9999 (LISTEN)\n"
    )
    monkeypatch.setattr(dash_server.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout))
    assert dash_server._listening_pid(9999) is None


def test_listening_pid_ignores_duplicate_fd_rows_for_same_pid(monkeypatch):
    # Same process, multiple listening FDs -- still an unambiguous single pid.
    stdout = (
        "COMMAND   PID   USER   FD   TYPE DEVICE SIZE/OFF NODE NAME\n"
        "python3  4321  user   6u  IPv4 0x0      0t0      TCP  *:9999 (LISTEN)\n"
        "python3  4321  user   7u  IPv6 0x1      0t0      TCP  *:9999 (LISTEN)\n"
    )
    monkeypatch.setattr(dash_server.subprocess, "run", lambda *a, **k: _FakeCompleted(stdout))
    assert dash_server._listening_pid(9999) == ("4321", "python3")


# --- _kill_and_wait ------------------------------------------------------------


def test_kill_and_wait_returns_true_once_sigterm_frees_the_port(monkeypatch, no_sleep, capsys):
    kill_calls = []
    monkeypatch.setattr(dash_server.os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))
    monkeypatch.setattr(dash_server, "_port_free", lambda port: True)

    ok = dash_server._kill_and_wait("4321", "python3", 9999)
    out = capsys.readouterr().out
    assert ok is True
    assert kill_calls == [(4321, dash_server.signal.SIGTERM)]
    assert "4321" in out and "python3" in out


def test_kill_and_wait_escalates_to_sigkill_when_sigterm_is_not_enough(monkeypatch, no_sleep):
    kill_calls = []
    # Behavior, not a poll count: busy until SIGKILL has actually been sent,
    # free from the very next check onward -- true regardless of how many
    # times `_kill_and_wait` polls before escalating.
    killed = {"sigkill_sent": False}

    def _free(_port):
        return killed["sigkill_sent"]

    def _kill_tracking(pid, sig):
        kill_calls.append((pid, sig))
        if sig == dash_server.signal.SIGKILL:
            killed["sigkill_sent"] = True

    monkeypatch.setattr(dash_server.os, "kill", _kill_tracking)
    monkeypatch.setattr(dash_server, "_port_free", _free)

    ok = dash_server._kill_and_wait("4321", "python3", 9999)
    assert ok is True
    assert kill_calls == [(4321, dash_server.signal.SIGTERM), (4321, dash_server.signal.SIGKILL)]
