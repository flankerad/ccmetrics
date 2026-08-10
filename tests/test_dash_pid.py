"""`dash/server.py`'s pid file -- `pid_path()`, `_write_pid_file`,
`_remove_pid_file`, and their wiring into `serve()`. This is what the
widget's restart button (widget.py's `_restart_worker`) reads before it ever
signals a process, so a stale or foreign file must never look valid, and a
clean shutdown must always clear it. `cc_env` points `CCMETRICS_DB` at a
tmp_path db, so `pid_path()` -- alongside that db's own dir -- never touches
the real ~/.local/share/ccmetrics.
"""

from __future__ import annotations

import json

from ccmetrics.dash import server as dash_server


class _FakeHTTPD:
    """Same stand-in as test_dash_serve_port_busy.py's -- serve_forever runs
    `on_serve` (if given) then raises KeyboardInterrupt so serve() unwinds
    through its own finally without a real event loop."""

    def __init__(self, on_serve=None):
        self.daemon_threads = None
        self.closed = False
        self._on_serve = on_serve

    def serve_forever(self):
        if self._on_serve:
            self._on_serve()
        raise KeyboardInterrupt

    def server_close(self):
        self.closed = True


def _bind_ok(_addr, _handler):
    return _FakeHTTPD()


# --- pid_path: alongside the store's own dir --------------------------------


def test_pid_path_sits_alongside_the_store_db(cc_env):
    assert dash_server.pid_path() == cc_env["db_path"].parent / "dash.pid"


# --- _write_pid_file / _remove_pid_file, called directly --------------------


def test_write_pid_file_records_pid_and_port(cc_env, monkeypatch):
    monkeypatch.setattr(dash_server.os, "getpid", lambda: 4242)
    dash_server._write_pid_file(9991)
    body = json.loads(dash_server.pid_path().read_text())
    assert body == {"pid": 4242, "port": 9991}


def test_write_pid_file_creates_a_missing_data_dir(tmp_path, monkeypatch):
    # CCMETRICS_DB pointed at a subpath whose directory has never been
    # created (no `store.connect()` call ran first to make it) -- the pid
    # write must not depend on that having already happened.
    nested = tmp_path / "nested" / "sub" / "state.db"
    monkeypatch.setenv("CCMETRICS_DB", str(nested))
    assert not nested.parent.exists()
    dash_server._write_pid_file(9991)
    assert dash_server.pid_path().parent == nested.parent
    assert json.loads(dash_server.pid_path().read_text())["port"] == 9991


def test_remove_pid_file_clears_its_own_pid(cc_env, monkeypatch):
    monkeypatch.setattr(dash_server.os, "getpid", lambda: 4242)
    dash_server._write_pid_file(9991)
    assert dash_server.pid_path().exists()
    dash_server._remove_pid_file()
    assert not dash_server.pid_path().exists()


def test_remove_pid_file_never_touches_a_different_pid(cc_env, monkeypatch):
    # A fresher `serve()` could have overwritten the file between this
    # process's write and its own shutdown -- removal must check the pid
    # inside the file still names this process before deleting it.
    dash_server.pid_path().parent.mkdir(parents=True, exist_ok=True)
    dash_server.pid_path().write_text(json.dumps({"pid": 99999999, "port": 9991}))
    monkeypatch.setattr(dash_server.os, "getpid", lambda: 4242)
    dash_server._remove_pid_file()
    assert dash_server.pid_path().exists()


def test_remove_pid_file_is_a_no_op_when_nothing_is_there(cc_env):
    assert not dash_server.pid_path().exists()
    dash_server._remove_pid_file()  # must not raise


# --- wired into serve() ------------------------------------------------------


def test_serve_writes_the_pid_file_before_serving(cc_env, monkeypatch):
    seen = []

    def _on_serve():
        seen.append(dash_server.pid_path().exists())

    monkeypatch.setattr(
        dash_server, "ThreadingHTTPServer", lambda _a, _h: _FakeHTTPD(_on_serve)
    )
    monkeypatch.setattr(dash_server.webbrowser, "open", lambda u: None)
    rc = dash_server.serve(port=9998, open_browser=False, reingest_period=None)
    assert rc == 0
    assert seen == [True]  # the pid file existed by the time serve_forever ran


def test_serve_removes_the_pid_file_on_clean_shutdown(cc_env, monkeypatch):
    monkeypatch.setattr(dash_server, "ThreadingHTTPServer", _bind_ok)
    monkeypatch.setattr(dash_server.webbrowser, "open", lambda u: None)
    rc = dash_server.serve(port=9999, open_browser=False, reingest_period=None)
    assert rc == 0
    assert not dash_server.pid_path().exists()
