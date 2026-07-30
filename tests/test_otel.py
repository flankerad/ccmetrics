"""OTEL exact-cost receiver (wave D): OTLP/JSON ingestion, dedupe, rejection,
metadata whitelist, exact/floor coverage math, v2->v3 migration, loopback bind."""

from __future__ import annotations

import datetime as _dt
import json
import socket
import sqlite3
import threading
import urllib.error
import urllib.request

import pytest

from ccmetrics import ingest, otel, report, store

from .util import assistant_rec, make_project, session_path, ts_at, write_lines

TODAY = _dt.date.today()
DAY0 = _dt.datetime.combine(TODAY - _dt.timedelta(days=2), _dt.time(12, 0, 0))
DAY1 = _dt.datetime.combine(TODAY - _dt.timedelta(days=1), _dt.time(12, 0, 0))


# --- OTLP/JSON payload builders ----------------------------------------------


def _av(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # proto3 JSON mapping encodes int64 as a *string* -- exercise that shape.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attr_list(attrs: dict) -> list:
    return [{"key": k, "value": _av(v)} for k, v in attrs.items() if v is not None]


def api_request_record(
    session_id: str = "sess-otel-1",
    *,
    cost_usd: float | None = 0.05,
    cost_usd_micros: float | None = None,
    model: str = "claude-sonnet-5",
    request_id: str | None = "req-1",
    ts: str = "2026-07-20T12:00:05.000Z",
    session_key: str = "session.id",
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 10,
    cache_write: int = 0,
    extra_attrs: dict | None = None,
) -> dict:
    attrs: dict = {
        "event.name": "api_request",
        session_key: session_id,
        "model": model,
        "event.timestamp": ts,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
    }
    if request_id is not None:
        attrs["request_id"] = request_id
    if cost_usd is not None:
        attrs["cost_usd"] = cost_usd
    if cost_usd_micros is not None:
        attrs["cost_usd_micros"] = cost_usd_micros
    if extra_attrs:
        attrs.update(extra_attrs)
    return {"attributes": _attr_list(attrs)}


def other_event_record(name: str = "some.other.event", extra_attrs: dict | None = None) -> dict:
    attrs = {"event.name": name}
    if extra_attrs:
        attrs.update(extra_attrs)
    return {"attributes": _attr_list(attrs)}


def logs_payload(records: list[dict], resource_attrs: dict | None = None) -> dict:
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": _attr_list(resource_attrs or {})},
                "scopeLogs": [{"logRecords": records}],
            }
        ]
    }


def _post(url: str, body: bytes, ctype: str = "application/json"):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _post_json(url: str, payload: dict):
    return _post(url, json.dumps(payload).encode("utf-8"))


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def otel_server(cc_env):
    port = _free_port()
    wconn = store.connect(cc_env["db_path"], check_same_thread=False)
    state = otel._State(wconn)
    handler = type("BoundHandler", (otel.Handler,), {"state": state, "verbose": False})
    httpd = otel.ThreadingHTTPServer((otel.HOST, port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", state, wconn, httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        wconn.close()


# --- 1. receiver: valid posts land, attribute spelling variants --------------


def test_receiver_valid_post_lands_row_with_session_dot_id(otel_server, conn):
    base, _state, _wconn, _httpd = otel_server
    payload = logs_payload(
        [api_request_record(session_id="s1", cost_usd=0.05, request_id="r1", session_key="session.id")]
    )
    status, body = _post_json(f"{base}/v1/logs", payload)
    assert status == 200
    row = conn.execute("SELECT * FROM otel_costs WHERE session_id=?", ("s1",)).fetchone()
    assert row is not None
    assert row["cost_usd"] == pytest.approx(0.05)
    assert row["input_tokens"] == 100


def test_receiver_cost_usd_micros_only_and_session_id_underscore_variant(otel_server, conn):
    base, _state, _wconn, _httpd = otel_server
    payload = logs_payload(
        [
            api_request_record(
                session_id="s2",
                cost_usd=None,
                cost_usd_micros=75000,
                request_id="r2",
                session_key="session_id",
            )
        ]
    )
    status, body = _post_json(f"{base}/v1/logs", payload)
    assert status == 200
    row = conn.execute("SELECT * FROM otel_costs WHERE session_id=?", ("s2",)).fetchone()
    assert row is not None
    assert row["cost_usd"] == pytest.approx(0.075)


# --- 2. dedupe -----------------------------------------------------------------


def test_receiver_identical_repost_does_not_duplicate(otel_server, conn):
    base, _state, _wconn, _httpd = otel_server
    payload = logs_payload([api_request_record(session_id="s3", request_id="req-dup")])
    body = json.dumps(payload).encode("utf-8")
    status1, _ = _post(f"{base}/v1/logs", body)
    status2, _ = _post(f"{base}/v1/logs", body)
    assert status1 == 200 and status2 == 200
    count = conn.execute(
        "SELECT COUNT(*) c FROM otel_costs WHERE session_id=?", ("s3",)
    ).fetchone()["c"]
    assert count == 1


def test_receiver_same_session_different_request_id_is_new_row(otel_server, conn):
    base, _state, _wconn, _httpd = otel_server
    payload_a = logs_payload([api_request_record(session_id="s4", request_id="req-a")])
    payload_b = logs_payload([api_request_record(session_id="s4", request_id="req-b")])
    _post_json(f"{base}/v1/logs", payload_a)
    _post_json(f"{base}/v1/logs", payload_b)
    count = conn.execute(
        "SELECT COUNT(*) c FROM otel_costs WHERE session_id=?", ("s4",)
    ).fetchone()["c"]
    assert count == 2


# --- 3. rejection ----------------------------------------------------------


def test_receiver_rejects_protobuf_content_type(otel_server):
    base, _state, _wconn, _httpd = otel_server
    status, body = _post(f"{base}/v1/logs", b"\x08\x01\x10\x02", ctype="application/x-protobuf")
    assert status == 415
    assert "hint" in body


def test_receiver_rejects_malformed_json(otel_server):
    base, _state, _wconn, _httpd = otel_server
    status, body = _post(f"{base}/v1/logs", b"{not valid json", ctype="application/json")
    assert status == 400


def test_receiver_no_whitelisted_events_is_200_and_zero_rows(otel_server, conn):
    base, _state, _wconn, _httpd = otel_server
    payload = logs_payload([other_event_record("session.started")])
    status, body = _post_json(f"{base}/v1/logs", payload)
    assert status == 200
    assert conn.execute("SELECT COUNT(*) c FROM otel_costs").fetchone()["c"] == 0


def test_receiver_survives_bad_posts_next_valid_post_still_lands(otel_server, conn):
    base, _state, _wconn, _httpd = otel_server
    s1, _ = _post(f"{base}/v1/logs", b"\x08\x01", ctype="application/x-protobuf")
    s2, _ = _post(f"{base}/v1/logs", b"{bad", ctype="application/json")
    s3, _ = _post_json(f"{base}/v1/logs", logs_payload([other_event_record()]))
    assert (s1, s2, s3) == (415, 400, 200)
    payload = logs_payload([api_request_record(session_id="s-survive", request_id="req-survive")])
    status, _ = _post_json(f"{base}/v1/logs", payload)
    assert status == 200
    row = conn.execute(
        "SELECT * FROM otel_costs WHERE session_id=?", ("s-survive",)
    ).fetchone()
    assert row is not None


# --- 4. metadata whitelist ------------------------------------------------


def test_metadata_whitelist_keeps_prompt_and_secrets_out_of_db(otel_server, conn, cc_env):
    base, _state, wconn, _httpd = otel_server
    canary = "SECRET_CANARY_OTEL"
    extra = {
        "prompt": f"do the thing {canary}",
        "user.email": "victim@example.com",
        "tool_input": f"rm -rf / {canary}",
        "body": f"raw api body {canary}",
    }
    payload = logs_payload(
        [api_request_record(session_id="s5", request_id="req-canary", extra_attrs=extra)]
    )
    status, _ = _post_json(f"{base}/v1/logs", payload)
    assert status == 200
    row = conn.execute("SELECT * FROM otel_costs WHERE session_id=?", ("s5",)).fetchone()
    assert row is not None  # the whitelisted fields still landed

    wconn.execute("PRAGMA wal_checkpoint(FULL)")
    db_path = cc_env["db_path"]
    blob = db_path.read_bytes()
    for suffix in ("-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            blob += p.read_bytes()
    assert canary.encode() not in blob
    assert b"victim@example.com" not in blob
    assert b"rm -rf" not in blob


# --- 5. coverage math: exact vs floor labelling -----------------------------


def _write_turns(cc_env, project_key: str, session_id: str, base_dt: _dt.datetime, n: int):
    proj = make_project(cc_env["projects_dir"], project_key)
    recs = [
        assistant_rec(
            session_id, f"m{i}", ts_at(base_dt, i * 5), "claude-haiku-4-5", cw5m=1000, cread=200
        )
        for i in range(n)
    ]
    write_lines(session_path(proj, session_id), recs)


def _otel_post(base_url: str, session_id: str, base_dt: _dt.datetime, n: int, start_i: int = 0):
    records = [
        api_request_record(
            session_id=session_id,
            cost_usd=0.10,
            request_id=f"req-{session_id}-{i}",
            ts=ts_at(base_dt, i * 5),
        )
        for i in range(start_i, start_i + n)
    ]
    payload = logs_payload(records)
    return _post_json(f"{base_url}/v1/logs", payload)


def test_fully_covered_day_labelled_exact_otel(otel_server, conn, cc_env):
    base, _state, _wconn, _httpd = otel_server
    project_key = "-Users-otel-full-proj"
    _write_turns(cc_env, project_key, "s-full", DAY0, 2)
    ingest.ingest(conn, cc_env["projects_dir"])

    status, _ = _otel_post(base, "s-full", DAY0, 2)
    assert status == 200

    date_str = DAY0.strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT exact_coverage, exact_usd, exact_events, turns FROM daily "
        "WHERE project=? AND date=?",
        (project_key, date_str),
    ).fetchone()
    assert row is not None
    assert row["turns"] == 2
    assert row["exact_events"] == 2
    assert row["exact_coverage"] >= 0.95
    assert row["exact_usd"] == pytest.approx(0.20)

    s = report.summary(conn, project_key)
    assert s["exact"]["mode"] == "exact"
    assert report.confidence_label(s["exact"]) == "exact (OTEL)"
    out = report.render(s, None, found=[])
    assert "exact (OTEL)" in out


def test_half_covered_day_stays_floor(otel_server, conn, cc_env):
    base, _state, _wconn, _httpd = otel_server
    project_key = "-Users-otel-half-proj"
    _write_turns(cc_env, project_key, "s-half", DAY0, 2)
    ingest.ingest(conn, cc_env["projects_dir"])

    status, _ = _otel_post(base, "s-half", DAY0, 1)  # 1 of 2 turns -> 50%
    assert status == 200

    date_str = DAY0.strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT exact_coverage FROM daily WHERE project=? AND date=?",
        (project_key, date_str),
    ).fetchone()
    assert row is not None
    assert row["exact_coverage"] == pytest.approx(0.5)
    assert row["exact_coverage"] < 0.95

    s = report.summary(conn, project_key)
    assert s["exact"]["mode"] != "exact"
    assert report.confidence_label(s["exact"]) != "exact (OTEL)"
    out = report.render(s, None, found=[])
    assert "stays a floor" in out or "≈" in out


def test_mixed_period_shows_both_exact_and_floor_numbers(otel_server, conn, cc_env):
    base, _state, _wconn, _httpd = otel_server
    project_key = "-Users-otel-mixed-proj"
    _write_turns(cc_env, project_key, "s-mixed-day0", DAY0, 2)
    _write_turns(cc_env, project_key, "s-mixed-day1", DAY1, 2)
    ingest.ingest(conn, cc_env["projects_dir"])

    # day0: fully covered; day1: half covered.
    s0, _ = _otel_post(base, "s-mixed-day0", DAY0, 2)
    s1, _ = _otel_post(base, "s-mixed-day1", DAY1, 1)
    assert s0 == 200 and s1 == 200

    s = report.summary(conn, project_key)
    exact = s["exact"]
    assert exact["mode"] == "mixed"
    assert exact["days_covered"] == 1
    assert exact["days_uncovered"] == 1
    assert exact["exact_usd"] > 0
    assert exact["floor_usd_uncovered"] > 0

    label = report.confidence_label(exact)
    assert label.startswith("mixed")
    out = report.render(s, None, found=[])
    assert "exact (OTEL)" in out
    assert "floor" in out


# --- 6. migration: v2 schema -> v3 in place ---------------------------------


def _build_v2_db(db_path) -> None:
    raw = sqlite3.connect(str(db_path))
    raw.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE daily (
            project   TEXT NOT NULL,
            date      TEXT NOT NULL,
            floor_usd REAL,
            cw5m      INTEGER NOT NULL DEFAULT 0,
            cw1h      INTEGER NOT NULL DEFAULT 0,
            cread     INTEGER NOT NULL DEFAULT 0,
            out_bytes INTEGER NOT NULL DEFAULT 0,
            turns     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (project, date)
        );
        """
    )
    raw.execute(
        "INSERT INTO daily(project,date,floor_usd,turns) VALUES(?,?,?,?)",
        ("-Users-old-proj", "2026-07-01", 1.23, 5),
    )
    raw.execute("INSERT INTO meta(key,value) VALUES('schema_version','2')")
    raw.commit()
    raw.close()


def test_migration_v2_db_upgrades_in_place(cc_env):
    _build_v2_db(cc_env["db_path"])

    c = store.connect(cc_env["db_path"])
    try:
        tables = {
            r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "otel_costs" in tables

        cols = {r["name"] for r in c.execute("PRAGMA table_info(daily)")}
        for col in ("exact_usd", "exact_events", "exact_coverage"):
            assert col in cols

        row = c.execute(
            "SELECT floor_usd, turns, exact_usd, exact_events, exact_coverage FROM daily "
            "WHERE project=? AND date=?",
            ("-Users-old-proj", "2026-07-01"),
        ).fetchone()
        assert row is not None
        assert row["floor_usd"] == pytest.approx(1.23)
        assert row["turns"] == 5
        assert row["exact_usd"] is None
        assert row["exact_events"] == 0

        assert store.get_meta(c, "schema_version") == str(store.SCHEMA_VERSION)
    finally:
        c.close()


# --- 7. bind: loopback only --------------------------------------------------


def test_otel_server_binds_localhost_only(otel_server):
    _base, _state, _wconn, httpd = otel_server
    assert httpd.server_address[0] == "127.0.0.1"
    assert otel.HOST == "127.0.0.1"
