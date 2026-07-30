"""Metadata-only invariant: a distinctive secret string placed in prompt text,
file content and tool-result bodies must appear NOWHERE in the DB file bytes,
and nowhere in any /api/* JSON payload."""

from __future__ import annotations

import datetime as _dt
import json
import socket
import threading
import time
import urllib.request

from ccmetrics import ingest, store
from ccmetrics.dash import server as dash_server

from .util import assistant_rec, make_project, session_path, tool_result_rec, ts_at, user_prompt_rec, write_lines

BASE = _dt.datetime(2026, 7, 20, 12, 0, 0)
CANARY = "SECRET_CANARY_XYZ"


def _build_canary_fixture(cc_env):
    proj = make_project(cc_env["projects_dir"], "-Users-secret-proj")
    f = session_path(proj, "s1")
    write_lines(
        f,
        [
            user_prompt_rec("s1", ts_at(BASE, 0), f"please read the file, it has {CANARY} inside"),
            assistant_rec(
                "s1", "m1", ts_at(BASE, 1), "claude-haiku-4-5",
                text=f"I found {CANARY} in the file and will fix it.",
                cread=100,
                tool_uses=[{
                    "id": "tu1", "name": "Write",
                    "input": {"file_path": "/tmp/secret.py", "content": f"# {CANARY}\nprint('x')"},
                }],
            ),
            tool_result_rec("s1", ts_at(BASE, 2), "tu1", f"wrote file containing {CANARY}"),
        ],
    )
    return proj


def test_canary_absent_from_db_bytes(conn, cc_env):
    _build_canary_fixture(cc_env)
    ingest.ingest(conn, cc_env["projects_dir"])
    from ccmetrics import detectors

    detectors.run_and_store(conn)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(FULL)")

    db_path = cc_env["db_path"]
    assert db_path.exists()
    raw = db_path.read_bytes()
    assert CANARY.encode() not in raw

    # also check the WAL file, if sqlite left one behind
    wal = db_path.with_suffix(db_path.suffix + "-wal")
    if wal.exists():
        assert CANARY.encode() not in wal.read_bytes()


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_canary_absent_from_api_payloads(conn, cc_env, monkeypatch):
    proj = _build_canary_fixture(cc_env)
    project_key = proj.name
    ingest.ingest(conn, cc_env["projects_dir"])
    from ccmetrics import detectors

    detectors.run_and_store(conn)
    conn.commit()

    port = _free_port()
    httpd = dash_server.ThreadingHTTPServer((dash_server.HOST, port), dash_server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        assert httpd.server_address[0] == "127.0.0.1"
        base = f"http://127.0.0.1:{port}"
        paths = [
            "/api/summary",
            "/api/projects",
            f"/api/project/{project_key}",
            "/api/findings",
            "/api/live",
            "/api/constants",
            "/api/meta",
        ]
        for p in paths:
            with urllib.request.urlopen(base + p, timeout=5) as resp:
                assert resp.status == 200
                body = resp.read()
                assert CANARY.encode() not in body, f"canary leaked from {p}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
