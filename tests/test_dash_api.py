"""Dash server: ephemeral port, 200 + JSON shape per endpoint, /api/live fresh
vs stale, binds 127.0.0.1 only."""

from __future__ import annotations

import datetime as _dt
import json
import os
import socket
import threading
import urllib.error
import urllib.request

import pytest

from ccmetrics import detectors, ingest
from ccmetrics.dash import server as dash_server

from .util import assistant_rec, make_project, session_path, ts_at, write_lines

BASE = _dt.datetime(2026, 7, 20, 12, 0, 0)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def running_server(cc_env):
    port = _free_port()
    httpd = dash_server.ThreadingHTTPServer((dash_server.HOST, port), dash_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _seed(conn, cc_env, project_key="-Users-dash-proj"):
    proj = make_project(cc_env["projects_dir"], project_key)
    write_lines(
        session_path(proj, "s1"),
        [
            assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=1000, cread=200),
            assistant_rec("s1", "m2", ts_at(BASE, 5), "claude-haiku-4-5", cread=200),
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    detectors.run_and_store(conn)
    conn.commit()
    return project_key


def _get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        assert "application/json" in resp.headers.get("Content-Type", "")
        return json.loads(resp.read())


def test_server_binds_localhost_only(running_server):
    base, httpd = running_server
    assert httpd.server_address[0] == "127.0.0.1"


def test_api_summary_shape(conn, cc_env, running_server):
    _seed(conn, cc_env)
    base, _ = running_server
    body = _get_json(f"{base}/api/summary")
    for key in ("scope", "window_days", "spend", "tokens", "totals", "per_model", "series"):
        assert key in body
    assert "floor_usd" in body["spend"]
    assert "cread" in body["tokens"]


def test_api_projects_shape(conn, cc_env, running_server):
    project_key = _seed(conn, cc_env)
    base, _ = running_server
    body = _get_json(f"{base}/api/projects")
    assert "rows" in body and "window_days" in body
    projects = [r["project"] for r in body["rows"]]
    assert project_key in projects


def test_api_project_key_shape(conn, cc_env, running_server):
    project_key = _seed(conn, cc_env)
    base, _ = running_server
    body = _get_json(f"{base}/api/project/{project_key}")
    assert body["scope"] == project_key
    assert "spend" in body and "tokens" in body


def test_api_findings_shape(conn, cc_env, running_server):
    _seed(conn, cc_env)
    base, _ = running_server
    body = _get_json(f"{base}/api/findings")
    assert "count" in body and "findings" in body
    if body["findings"]:
        f = body["findings"][0]
        for key in ("detector", "name", "tokens_saved", "effort", "fix_text", "evidence"):
            assert key in f


def test_api_constants_shape(conn, cc_env, running_server):
    base, _ = running_server
    body = _get_json(f"{base}/api/constants")
    assert "constants" in body
    assert len(body["constants"]) > 0
    assert "source_url" in body["constants"][0]


def test_api_meta_shape(conn, cc_env, running_server):
    _seed(conn, cc_env)
    base, _ = running_server
    body = _get_json(f"{base}/api/meta")
    for key in ("version", "db_path", "schema_version", "window_days"):
        assert key in body


def test_api_unknown_path_404(running_server):
    base, _ = running_server
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{base}/api/nope", timeout=5)
    assert exc_info.value.code == 404


def test_api_live_fresh_session(conn, cc_env, running_server):
    proj = make_project(cc_env["projects_dir"], "-Users-live-proj")
    write_lines(
        session_path(proj, "s1"),
        [assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=100)],
    )
    base, _ = running_server
    body = _get_json(f"{base}/api/live")
    assert body["status"] == "live"
    assert body["turns"] == 1


def test_api_live_stale_session(conn, cc_env, running_server):
    proj = make_project(cc_env["projects_dir"], "-Users-live-stale-proj")
    f = session_path(proj, "s1")
    write_lines(f, [assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=100)])
    stale_time = _dt.datetime.now().timestamp() - 400  # past the 300s staleness window
    os.utime(f, (stale_time, stale_time))

    base, _ = running_server
    body = _get_json(f"{base}/api/live")
    assert body["status"] == "no live session"
