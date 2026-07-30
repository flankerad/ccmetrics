"""One focused test per detector (1-12): a fixture built to fire it, and a
near-miss fixture at the threshold edge that must NOT fire."""

from __future__ import annotations

import datetime as _dt
import json

from ccmetrics import detectors, ingest

from .util import (
    assistant_rec,
    compact_rec,
    hook_rec,
    make_project,
    session_path,
    tool_result_rec,
    ts_at,
    user_prompt_rec,
    write_lines,
)

BASE = _dt.datetime(2026, 7, 20, 12, 0, 0)


def _findings_for(conn, project_key, detector_num):
    from ccmetrics.detectors import Ctx, DETECTORS

    ctx = Ctx(conn)
    return DETECTORS[detector_num](ctx)


def _ingest_and_findings(conn, cc_env, detector_num, project_key=None):
    ingest.ingest(conn, cc_env["projects_dir"])
    return _findings_for(conn, project_key, detector_num)


def _proj_findings(findings, project_key):
    return [f for f in findings if f["project"] == project_key]


# --- 1. cache-miss on idle gap -----------------------------------------------


def test_d1_idle_gap_fires(conn, cc_env):
    proj_key = "proj-d1-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    f = session_path(proj, "s1")
    write_lines(
        f,
        [
            assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=100),
            # gap > 3600s, rewrite >= 10000 tokens: fires
            assistant_rec("s1", "m2", ts_at(BASE, 3700), "claude-haiku-4-5", cw5m=10_000),
        ],
    )
    findings = _ingest_and_findings(conn, cc_env, 1)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d1_idle_gap_near_miss_short_gap(conn, cc_env):
    proj_key = "proj-d1-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    f = session_path(proj, "s1")
    write_lines(
        f,
        [
            assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=100),
            # gap well under 3600s: must not fire despite a big rewrite
            assistant_rec("s1", "m2", ts_at(BASE, 1000), "claude-haiku-4-5", cw5m=10_000),
        ],
    )
    findings = _ingest_and_findings(conn, cc_env, 1)
    assert _proj_findings(findings, proj_key) == []


# --- 2. compaction tax --------------------------------------------------------


def test_d2_compaction_fires(conn, cc_env):
    proj_key = "proj-d2-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    f = session_path(proj, "s1")
    write_lines(
        f,
        [
            assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10),
            compact_rec("s1", ts_at(BASE, 10), pre_tokens=5000),
            compact_rec("s1", ts_at(BASE, 20), pre_tokens=5000),  # 2nd compaction: >= min
        ],
    )
    findings = _ingest_and_findings(conn, cc_env, 2)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d2_compaction_near_miss_single_compaction(conn, cc_env):
    proj_key = "proj-d2-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    f = session_path(proj, "s1")
    write_lines(
        f,
        [
            assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=10),
            compact_rec("s1", ts_at(BASE, 10), pre_tokens=5000),  # only 1: below min(2)
        ],
    )
    findings = _ingest_and_findings(conn, cc_env, 2)
    assert _proj_findings(findings, proj_key) == []


# --- 3. context bloat ---------------------------------------------------------


def _make_d3_session(proj, sid, n_turns, cread_start, cread_step, day_offset):
    recs = []
    for i in range(n_turns):
        ts = ts_at(BASE, day_offset * 86400 + i * 5)
        recs.append(
            assistant_rec(sid, f"{sid}-m{i}", ts, "claude-haiku-4-5", cread=cread_start + i * cread_step)
        )
    write_lines(session_path(proj, sid), recs)


def test_d3_context_bloat_fires(conn, cc_env):
    proj_key = "proj-d3-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    # 4 "normal" sessions with a shallow cread slope, 1 outlier with a steep one
    for i in range(4):
        _make_d3_session(proj, f"norm{i}", 10, 100, 20, day_offset=i)
    _make_d3_session(proj, "steep", 10, 100, 2000, day_offset=9)

    findings = _ingest_and_findings(conn, cc_env, 3)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    ev = json.loads(hits[0]["evidence"])
    assert "steep" in ev["session_ids"]
    assert hits[0]["tokens_saved"] > 0


def test_d3_context_bloat_near_miss_too_few_sessions(conn, cc_env):
    proj_key = "proj-d3-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    # only 4 qualifying sessions -- below d3_min_sessions (5): must not fire
    for i in range(3):
        _make_d3_session(proj, f"norm{i}", 10, 100, 20, day_offset=i)
    _make_d3_session(proj, "steep", 10, 100, 2000, day_offset=9)

    findings = _ingest_and_findings(conn, cc_env, 3)
    assert _proj_findings(findings, proj_key) == []


# --- 4. cache thrash -----------------------------------------------------------


def test_d4_cache_thrash_fires(conn, cc_env):
    proj_key = "proj-d4-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(5):  # >= d4_min_turns
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5",
                cw5m=20_000, cread=200,  # 100K written total, far under breakeven reads
            )
        )
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 4)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d4_cache_thrash_near_miss_well_amortised(conn, cc_env):
    proj_key = "proj-d4-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(5):
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5",
                cw5m=20_000, cread=200_000,  # reads well exceed breakeven (cw5m*2)
            )
        )
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 4)
    assert _proj_findings(findings, proj_key) == []


# --- 5. model mis-routing -----------------------------------------------------


def _make_d5_turns(proj, sid, model, n=20):
    recs = [user_prompt_rec(sid, ts_at(BASE, 0), "hi")]
    for i in range(n):
        recs.append(assistant_rec(sid, f"{sid}-m{i}", ts_at(BASE, i * 5 + 1), model, cread=10))
    write_lines(session_path(proj, sid), recs)


def test_d5_model_misrouting_fires(conn, cc_env):
    proj_key = "proj-d5-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    _make_d5_turns(proj, "s1", "claude-opus-5", n=20)  # premium model, small turns
    findings = _ingest_and_findings(conn, cc_env, 5)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d5_model_misrouting_near_miss_already_cheap(conn, cc_env):
    proj_key = "proj-d5-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    _make_d5_turns(proj, "s1", "claude-haiku-4-5", n=20)  # already the cheap model
    findings = _ingest_and_findings(conn, cc_env, 5)
    assert _proj_findings(findings, proj_key) == []


# --- 6. repeated / oversized tool results -------------------------------------


def test_d6_repeated_results_fires(conn, cc_env):
    proj_key = "proj-d6-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(3):  # >= d6_repeat_calls
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5",
                tool_uses=[{"id": f"tu{i}", "name": "Grep", "input": {"pattern": "TODO"}}],
            )
        )
        recs.append(tool_result_rec("s1", ts_at(BASE, i * 5 + 1), f"tu{i}", "x" * 400))
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 6)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d6_repeated_results_near_miss_only_twice(conn, cc_env):
    proj_key = "proj-d6-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(2):  # below d6_repeat_calls (3)
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5",
                tool_uses=[{"id": f"tu{i}", "name": "Grep", "input": {"pattern": "TODO"}}],
            )
        )
        recs.append(tool_result_rec("s1", ts_at(BASE, i * 5 + 1), f"tu{i}", "x" * 400))
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 6)
    assert _proj_findings(findings, proj_key) == []


# --- 7. hook & denial overhead -------------------------------------------------


def test_d7_hook_denial_fires(conn, cc_env):
    proj_key = "proj-d7-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = [
        assistant_rec(
            "s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5",
            tool_uses=[{"id": "tu1", "name": "Bash", "input": {"command": "rm -rf x"}}],
        ),
        tool_result_rec("s1", ts_at(BASE, 1), "tu1", "permission denied", is_error=True,
                         denial_kind="rule"),
    ]
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 7)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d7_hook_denial_near_miss_clean_session(conn, cc_env):
    proj_key = "proj-d7-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = [
        assistant_rec(
            "s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5",
            tool_uses=[{"id": "tu1", "name": "Bash", "input": {"command": "ls"}}],
        ),
        tool_result_rec("s1", ts_at(BASE, 1), "tu1", "ok", is_error=False),
    ]
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 7)
    assert _proj_findings(findings, proj_key) == []


# --- 8. unproductive sidechains -------------------------------------------------


def test_d8_unproductive_sidechains_fires(conn, cc_env):
    proj_key = "proj-d8-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(3):  # >= d8_min_sidechain_turns
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5", cread=100,
                sidechain=True,
                tool_uses=[{"id": f"tu{i}", "name": "Read", "input": {"file_path": "/a.py"}}],
            )
        )
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 8)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d8_unproductive_sidechains_near_miss_has_edit(conn, cc_env):
    proj_key = "proj-d8-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(3):
        tool = {"id": f"tu{i}", "name": "Read", "input": {"file_path": "/a.py"}}
        if i == 1:
            tool = {"id": f"tu{i}", "name": "Edit", "input": {"file_path": "/a.py"}}
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5", cread=100,
                sidechain=True, tool_uses=[tool],
            )
        )
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 8)
    assert _proj_findings(findings, proj_key) == []


# --- 9. agent-team fan-out -------------------------------------------------------


def _d9_task_calls(n):
    return [{"id": f"task{i}", "name": "Task", "input": {"prompt": f"do {i}"}} for i in range(n)]


def test_d9_fanout_fires(conn, cc_env):
    proj_key = "proj-d9-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = [
        assistant_rec("s1", "planner", ts_at(BASE, 0), "claude-haiku-4-5",
                       tool_uses=_d9_task_calls(8)),  # > d9_expected_fanout (7)
    ]
    for i in range(3):
        recs.append(
            assistant_rec("s1", f"sub{i}", ts_at(BASE, i + 1), "claude-haiku-4-5",
                           cread=1000, sidechain=True)
        )
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 9)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] >= 0


def test_d9_fanout_near_miss_at_baseline(conn, cc_env):
    proj_key = "proj-d9-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = [
        assistant_rec("s1", "planner", ts_at(BASE, 0), "claude-haiku-4-5",
                       tool_uses=_d9_task_calls(7)),  # exactly at baseline: not > 7
    ]
    for i in range(3):
        recs.append(
            assistant_rec("s1", f"sub{i}", ts_at(BASE, i + 1), "claude-haiku-4-5",
                           cread=1000, sidechain=True)
        )
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 9)
    assert _proj_findings(findings, proj_key) == []


# --- 10. phantom idle spend -------------------------------------------------------


def test_d10_phantom_idle_fires(conn, cc_env):
    proj_key = "proj-d10-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    # no preceding user prompt line at all: prompt_bytes stays NULL
    write_lines(
        session_path(proj, "s1"),
        [assistant_rec("s1", "m1", ts_at(BASE, 0), "claude-haiku-4-5", cread=1000)],
    )
    findings = _ingest_and_findings(conn, cc_env, 10)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d10_phantom_idle_near_miss_with_prompt(conn, cc_env):
    proj_key = "proj-d10-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    write_lines(
        session_path(proj, "s1"),
        [
            user_prompt_rec("s1", ts_at(BASE, 0), "hello"),
            assistant_rec("s1", "m1", ts_at(BASE, 1), "claude-haiku-4-5", cread=1000),
        ],
    )
    findings = _ingest_and_findings(conn, cc_env, 10)
    assert _proj_findings(findings, proj_key) == []


# --- 11. burn-rate spike -------------------------------------------------------


def test_d11_burn_spike_fires(conn, cc_env):
    proj_key = "proj-d11-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(10):  # >= d11_min_preceding
        recs.append(assistant_rec("s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5", cread=1000))
    # 11th turn burns far above the p90 of the preceding 10
    recs.append(assistant_rec("s1", "m10", ts_at(BASE, 55), "claude-haiku-4-5", cread=100_000))
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 11)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d11_burn_spike_near_miss_consistent_burn(conn, cc_env):
    proj_key = "proj-d11-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(11):  # same shape, but every turn identical -> no spike
        recs.append(assistant_rec("s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5", cread=1000))
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 11)
    assert _proj_findings(findings, proj_key) == []


# --- 12. file re-read waste -------------------------------------------------------


def test_d12_file_rereads_fires_three_reads_no_edit(conn, cc_env):
    proj_key = "proj-d12-hit"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = []
    for i in range(3):
        recs.append(
            assistant_rec(
                "s1", f"m{i}", ts_at(BASE, i * 5), "claude-haiku-4-5",
                tool_uses=[{"id": f"tu{i}", "name": "Read", "input": {"file_path": "/a.py"}}],
            )
        )
        recs.append(tool_result_rec("s1", ts_at(BASE, i * 5 + 1), f"tu{i}", "y" * 400))
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 12)
    hits = _proj_findings(findings, proj_key)
    assert len(hits) == 1
    assert hits[0]["tokens_saved"] > 0


def test_d12_file_rereads_near_miss_read_edit_read_read(conn, cc_env):
    """read, edit, read, read -> the edit breaks the run so only 2 reads
    accumulate after it: below the 3-read threshold, must NOT fire."""
    proj_key = "proj-d12-miss"
    proj = make_project(cc_env["projects_dir"], proj_key)
    recs = [
        assistant_rec("s1", "m0", ts_at(BASE, 0), "claude-haiku-4-5",
                       tool_uses=[{"id": "tu0", "name": "Read", "input": {"file_path": "/a.py"}}]),
        tool_result_rec("s1", ts_at(BASE, 1), "tu0", "y" * 400),
        assistant_rec("s1", "m1", ts_at(BASE, 2), "claude-haiku-4-5",
                       tool_uses=[{"id": "tu1", "name": "Edit", "input": {"file_path": "/a.py"}}]),
        tool_result_rec("s1", ts_at(BASE, 3), "tu1", "edited"),
        assistant_rec("s1", "m2", ts_at(BASE, 4), "claude-haiku-4-5",
                       tool_uses=[{"id": "tu2", "name": "Read", "input": {"file_path": "/a.py"}}]),
        tool_result_rec("s1", ts_at(BASE, 5), "tu2", "y" * 400),
        assistant_rec("s1", "m3", ts_at(BASE, 6), "claude-haiku-4-5",
                       tool_uses=[{"id": "tu3", "name": "Read", "input": {"file_path": "/a.py"}}]),
        tool_result_rec("s1", ts_at(BASE, 7), "tu3", "y" * 400),
    ]
    write_lines(session_path(proj, "s1"), recs)
    findings = _ingest_and_findings(conn, cc_env, 12)
    assert _proj_findings(findings, proj_key) == []
