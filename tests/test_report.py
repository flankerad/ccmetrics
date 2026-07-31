"""Console report: floor $ shown, per-repo scoping via cwd, 'unknown' shown
when a model's rate is missing -- never a made-up number."""

from __future__ import annotations

import datetime as _dt

from ccmetrics import ingest, report

from .util import assistant_rec, make_project, session_path, ts_at, write_lines

BASE = _dt.datetime(2026, 7, 20, 12, 0, 0)


def test_report_contains_floor_dollar_figure(conn, cc_env):
    proj = make_project(cc_env["projects_dir"], "-Users-report-proj")
    write_lines(
        session_path(proj, "s1"),
        [assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=1000, cread=200)],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    s = report.summary(conn, "-Users-report-proj")
    out = report.render(s, None, found=[])
    assert "SPEND" in out
    assert "at least" in out
    assert s["floor_usd"] > 0
    assert s["floor_priced"] is True


def test_report_per_repo_scoping_matches_cwd(conn, cc_env, monkeypatch):
    # ingest.encode_project() turns an absolute cwd into the same '-'-joined
    # key Claude Code uses for its project directories.
    fake_cwd = "/Users/tester/my-repo"
    project_key = ingest.encode_project(fake_cwd)

    proj = make_project(cc_env["projects_dir"], project_key)
    write_lines(
        session_path(proj, "s1"),
        [assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=500, cread=50)],
    )
    other = make_project(cc_env["projects_dir"], "-Users-tester-other-repo")
    write_lines(
        session_path(other, "s2"),
        [assistant_rec("s2", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cw5m=999_000, cread=999_000)],
    )
    ingest.ingest(conn, cc_env["projects_dir"])

    monkeypatch.setattr("os.getcwd", lambda: fake_cwd)
    here = report.current_project_key()
    assert here == project_key
    assert here in report.known_projects(conn)

    s = report.summary(conn, here)
    # scoped strictly to this repo's tokens, not the much larger other project
    assert s["totals"]["cread"] == 50


def test_report_unknown_rate_shown_as_unknown_never_fabricated(conn, cc_env):
    proj = make_project(cc_env["projects_dir"], "-Users-unknown-model-proj")
    write_lines(
        session_path(proj, "s1"),
        [assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-mystery-model", cw5m=1000, cread=200)],
    )
    ingest.ingest(conn, cc_env["projects_dir"])
    s = report.summary(conn, "-Users-unknown-model-proj")
    assert s["floor_priced"] is False
    assert s["floor_unknown_equiv_tokens"] > 0
    # the unpriced model's floor_usd contribution must be None -> excluded, not 0
    unpriced = [m for m in s["per_model"] if m["model"] == "claude-mystery-model"]
    assert unpriced and unpriced[0]["floor_usd"] is None

    out = report.render(s, None, found=[])
    assert "unknown" in out
