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

from ccmetrics import costs, detectors, ingest
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


# --- DETECTOR_HELP + findings/projects payload help + ephemeral grouping ----


def test_detector_help_keys_complete():
    assert set(detectors.DETECTOR_HELP) == set(range(1, 13))
    for detector, text in detectors.DETECTOR_HELP.items():
        assert isinstance(text, str)
        assert text.strip(), f"detector {detector} help is empty"


def _seed_d1_finding(conn, cc_env, project_key="-Users-dash-d1-proj"):
    """A session with a >1h idle gap and a big rewrite: reliably trips
    detector 1 (cold start after a break), which is a headline finding."""
    proj = make_project(cc_env["projects_dir"], project_key)
    write_lines(
        session_path(proj, "s1"),
        [
            assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=100),
            assistant_rec("s1", "m2", ts_at(BASE, 7200), "claude-haiku-4-5", cw5m=15000),
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    detectors.run_and_store(conn)
    conn.commit()
    return project_key


def test_findings_payload_help_matches_detector_help(conn, cc_env):
    _seed_d1_finding(conn, cc_env)
    body = dash_server.findings_payload(conn, None)
    assert body["findings"], "expected the seeded idle-gap session to produce a finding"
    for f in body["findings"]:
        expected = detectors.DETECTOR_HELP[f["detector"]]
        assert f["help"] == expected
        assert f["help"]


def test_projects_payload_groups_ephemeral_temp_projects(conn, cc_env):
    real_key = "-Users-dash-real-proj"
    temp_key_1 = "-private-var-folders-xx-T-pytest-of-u-pytest-1-foo"
    temp_key_2 = "-tmp-scratch1"

    real_proj = make_project(cc_env["projects_dir"], real_key)
    write_lines(
        session_path(real_proj, "s-real"),
        [assistant_rec("s-real", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=1000, cread=200)],
    )

    temp_proj_1 = make_project(cc_env["projects_dir"], temp_key_1)
    write_lines(
        session_path(temp_proj_1, "s-temp1"),
        [assistant_rec("s-temp1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=2000)],
    )

    temp_proj_2 = make_project(cc_env["projects_dir"], temp_key_2)
    write_lines(
        session_path(temp_proj_2, "s-temp2"),
        [assistant_rec("s-temp2", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=500)],
    )

    ingest.ingest(conn, cc_env["projects_dir"])
    detectors.run_and_store(conn)
    conn.commit()

    body = dash_server.projects_payload(conn)
    rows = body["rows"]

    project_keys = [r["project"] for r in rows if r.get("project")]
    assert real_key in project_keys
    assert temp_key_1 not in project_keys
    assert temp_key_2 not in project_keys

    eph_rows = [r for r in rows if r.get("ephemeral")]
    assert len(eph_rows) == 1
    eph = eph_rows[0]
    assert eph["eph_count"] == 2
    assert len(eph["children"]) == 2

    child_projects = {c["project"] for c in eph["children"]}
    assert child_projects == {temp_key_1, temp_key_2}

    equivs = [c["equiv"] for c in eph["children"]]
    assert equivs == sorted(equivs, reverse=True)


# --- usage_payload: periods / per_model / repo_share -------------------------


def test_usage_payload_periods_and_global_sums(conn, cc_env):
    now = _dt.datetime.now()
    proj = make_project(cc_env["projects_dir"], "-Users-usage-global-proj")
    write_lines(
        session_path(proj, "s1"),
        [
            assistant_rec("s1", "m1", ts_at(now, 0), "claude-haiku-4-5", cw5m=1000, cread=200),
            assistant_rec("s1", "m2", ts_at(now, 60), "claude-haiku-4-5", cw5m=500, cread=100),
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    detectors.run_and_store(conn)
    conn.commit()

    body = dash_server.usage_payload(conn, None)
    periods = body["periods"]
    assert len(periods["day"]) == 14
    assert len(periods["week"]) == 8
    assert len(periods["month"]) == 6

    today = _dt.date.today().isoformat()
    for kind in ("day", "week", "month"):
        assert periods[kind][-1]["end"] <= today

    today_bucket = periods["day"][-1]
    assert today_bucket["start"] == today_bucket["end"] == today
    assert today_bucket["turns"] == 2
    expected_equiv = costs.billable_input_equivalent(1500, 0, 300)
    assert today_bucket["equiv_tokens"] == pytest.approx(expected_equiv)


def test_usage_payload_per_model_sorted_share_and_retention(conn, cc_env):
    now = _dt.datetime.now()
    proj = make_project(cc_env["projects_dir"], "-Users-usage-model-proj")
    write_lines(
        session_path(proj, "s1"),
        [
            # bigger model: two turns inside the 7d window
            assistant_rec("s1", "m1", ts_at(now, 0), "claude-opus-4", cw5m=4000, cread=1000),
            assistant_rec("s1", "m2", ts_at(now, -2 * 86400), "claude-opus-4", cw5m=1000, cread=200),
            # smaller model: one turn inside the 7d window
            assistant_rec("s1", "m3", ts_at(now, -1 * 86400), "claude-haiku-4-5", cw5m=200, cread=50),
            # beyond the 30-day turn retention: store.prune() deletes this from
            # `turns` during ingest, so per_model (sourced from `turns`) must
            # never see it, even though `daily` keeps it forever.
            assistant_rec(
                "s1", "m4", ts_at(now, -40 * 86400), "claude-sonnet-4-5", cw5m=9000, cread=9000
            ),
        ],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    detectors.run_and_store(conn)
    conn.commit()

    body = dash_server.usage_payload(conn, None)

    for window in ("7d", "30d"):
        rows = body["per_model"][window]
        models = {r["model"] for r in rows}
        assert "claude-sonnet-4-5" not in models  # pruned turn, older than 30d

        equivs = [r["equiv_tokens"] for r in rows]
        assert equivs == sorted(equivs, reverse=True)

        total_share = sum(r["share_pct"] for r in rows if r["share_pct"] is not None)
        assert total_share == pytest.approx(100.0, abs=0.5)

    seven_d_models = {r["model"] for r in body["per_model"]["7d"]}
    assert seven_d_models == {"claude-opus-4", "claude-haiku-4-5"}


def test_usage_payload_repo_share_ephemeral_and_scoping(conn, cc_env):
    now = _dt.datetime.now()
    real_key = "-Users-usage-repo-proj"
    temp_key = "-tmp-usage-scratch"

    real_proj = make_project(cc_env["projects_dir"], real_key)
    write_lines(
        session_path(real_proj, "s-real"),
        [assistant_rec("s-real", "m1", ts_at(now, 0), "claude-real-model", cw5m=2000, cread=500)],
    )

    temp_proj = make_project(cc_env["projects_dir"], temp_key)
    write_lines(
        session_path(temp_proj, "s-temp"),
        [assistant_rec("s-temp", "m1", ts_at(now, 0), "claude-temp-model", cw5m=300, cread=50)],
    )

    ingest.ingest(conn, cc_env["projects_dir"])
    detectors.run_and_store(conn)
    conn.commit()

    body = dash_server.usage_payload(conn, None)
    assert "repo_share" in body
    for kind in ("day", "week", "month"):
        rows = body["repo_share"][kind]["rows"]
        projects = [r["project"] for r in rows if not r.get("ephemeral")]
        assert real_key in projects
        assert temp_key not in projects  # never a per-repo row of its own

        eph_rows = [r for r in rows if r.get("ephemeral")]
        assert len(eph_rows) == 1
        assert eph_rows[0]["eph_count"] == 1

        total_share = sum(r["share_pct"] for r in rows if r["share_pct"] is not None)
        assert total_share == pytest.approx(100.0, abs=0.5)

    scoped = dash_server.usage_payload(conn, real_key)
    assert "repo_share" not in scoped
    for window in ("7d", "30d"):
        models = {r["model"] for r in scoped["per_model"][window]}
        assert models == {"claude-real-model"}
