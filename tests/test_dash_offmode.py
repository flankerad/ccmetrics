"""14-offmode-render-test: proves the caps-unknown page renders honestly at
RUNTIME, not just as strings in source. Extracts the page's <script> block,
runs it under node with a stubbed DOM + fetch, reads each host's innerHTML.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parent.parent / "ccmetrics" / "dash" / "static" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

HOST_IDS = [
    "cc-header", "cc-hero", "cc-recoverable", "cc-week", "cc-live",
    "cc-horizon", "cc-projects", "cc-dollars", "cc-footer",
]

HARNESS_HEAD = """
'use strict';
function makeHost(id) {
  return {
    id: id, innerHTML: "",
    querySelectorAll: function () { return []; },
    addEventListener: function () {},
    getAttribute: function () { return null; }
  };
}
var HOST_IDS = __HOST_IDS__;
var hosts = {};
HOST_IDS.forEach(function (id) { hosts[id] = makeHost(id); });
global.document = { getElementById: function (id) { return hosts[id] || makeHost(id); } };
global.window = global;
global.localStorage = { getItem: function () { return null; }, setItem: function () {} };
Object.defineProperty(global, "navigator", { value: {}, configurable: true, writable: true });
global.setInterval = function () { return 0; };
global.setTimeout = function () { return 0; };

var PAYLOADS = __PAYLOADS__;
function payloadFor(url) {
  var path = String(url).split("?")[0].replace(/^\\/api\\//, "");
  if (path === "projects") return PAYLOADS.projects;
  if (path.indexOf("project/") === 0) return PAYLOADS.summary;
  if (path === "windows") return PAYLOADS.windows;
  if (path === "live") return PAYLOADS.live;
  if (path === "findings") return PAYLOADS.findings;
  if (path === "summary") return PAYLOADS.summary;
  if (path === "meta") return PAYLOADS.meta;
  throw new Error("unmapped fetch: " + url);
}
global.fetch = function (url) {
  return Promise.resolve({ ok: true, json: function () { return Promise.resolve(payloadFor(url)); } });
};
"""

HARNESS_TAIL = """
setImmediate(function () {
  setImmediate(function () {
    setImmediate(function () {
      setImmediate(function () {
        var out = {};
        HOST_IDS.forEach(function (id) { out[id] = hosts[id].innerHTML; });
        console.log(JSON.stringify(out));
      });
    });
  });
});
"""


def _extract_script() -> str:
    text = INDEX.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", text, re.S)
    assert blocks, "no <script> block found in index.html"
    return max(blocks, key=len)


def _render(tmp_path: Path, payloads: dict) -> dict:
    head = HARNESS_HEAD.replace("__HOST_IDS__", json.dumps(HOST_IDS)).replace(
        "__PAYLOADS__", json.dumps(payloads)
    )
    js = head + "\n" + _extract_script() + "\n" + HARNESS_TAIL
    harness_path = tmp_path / "harness.js"
    harness_path.write_text(js, encoding="utf-8")
    result = subprocess.run(
        ["node", str(harness_path)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


COMMON = {
    "projects": {"rows": []}, "live": {"status": "idle"},
    "findings": {"findings": []}, "summary": {"series": []},
    "meta": {"version": "0.0.0", "corpus_files": 0},
}
CAPS_UNKNOWN_WINDOWS = {
    "scope": None, "generated": "2024-01-01T00:00:00Z", "caps_known": False,
    "caps": {}, "hook_ran": False, "setup_cmd": "ccmetrics statusline --setup",
    "anchor": None,
    "hero": {
        "used_pct": None, "clock_pct": 42.5, "reading_age_hours": None, "behind": None,
        "burnt_equiv": None, "left_equiv": None, "runs_out_at": None, "early_hours": None,
        "resets_at": None, "burn_equiv_per_hour": None, "burn_usd_per_hour": None,
        "pct_of_block_per_hour": None,
    },
    "headroom": [], "week": [], "week_blocks": 0, "current_block": None, "heaviest": [],
    "horizon": [], "dry_count_week": None, "dry_count_month": None, "led_counts": {},
    "block_hours": 5, "horizon_days": 30, "slot_labels": [], "tier": None, "credits": None,
    "source": None, "fetched_at": None,
}
CAPS_KNOWN_WINDOWS = {
    **CAPS_UNKNOWN_WINDOWS,
    "caps_known": True, "hook_ran": True, "caps": {"session": {"cap_equiv": 1_000_000}},
    "hero": {
        "used_pct": 63.2, "clock_pct": 50.0, "reading_age_hours": 1.0, "behind": 13.2,
        "burnt_equiv": 500000.0, "left_equiv": 300000.0, "runs_out_at": None,
        "early_hours": None, "resets_at": "2024-01-08T00:00:00Z",
        "burn_equiv_per_hour": 1000.0, "burn_usd_per_hour": 5.5, "pct_of_block_per_hour": 2.0,
    },
}


def test_offmode_renders_honestly(tmp_path):
    rendered = _render(tmp_path, dict(COMMON, windows=CAPS_UNKNOWN_WINDOWS))

    full = "\n".join(rendered.values())
    assert "undefined" not in full
    assert "NaN" not in full
    assert "[object Object]" not in full
    assert "could not reach the dash server" not in full  # boot() actually resolved

    hero = rendered["cc-hero"]
    assert "Caps are unknown until Claude Code" in hero
    assert "—" in hero  # em dash figure standing in for the missing number
    stat_row = hero[hero.index("BURNT"):]
    assert "%" not in stat_row

    assert "CAP UNKNOWN" in rendered["cc-week"]
    assert "CAP UNKNOWN" in rendered["cc-horizon"]


def test_capsknown_renders_numbers(tmp_path):
    rendered = _render(tmp_path, dict(COMMON, windows=CAPS_KNOWN_WINDOWS))
    hero = rendered["cc-hero"]
    stat_row = hero[hero.index("BURNT"):]
    assert "%" in stat_row


def _week_row(left_pct):
    return {
        "model": None, "label": "week all models", "short_label": "week",
        "left_pct": left_pct, "resets_at": "2024-01-08T00:00:00Z",
        "cap_equiv": 1_000_000, "used_pct": 100 - left_pct, "note": "",
    }


def test_tight_all_models_week_row_advises_ease_off(tmp_path):
    """A headroom row with no `model` (the all-models week row) still has to
    raise a warning when it's the only thing that's tight -- otherwise the
    panel falsely claims every limit has room."""
    windows = {**CAPS_KNOWN_WINDOWS, "headroom": [_week_row(8)]}
    rendered = _render(tmp_path, dict(COMMON, windows=windows))
    hero = rendered["cc-hero"]
    assert "NOTHING TO PUT DOWN" not in hero
    assert "&gt; EASE OFF — WEEK <span" in hero
    assert ">8%</span> LEFT UNTIL MON<" in hero


def test_all_models_week_row_with_room_still_says_nothing_to_put_down(tmp_path):
    """The negative case: a modelless row that has plenty of room must not
    trip the new branch -- "nothing to put down" still has to be honest."""
    windows = {**CAPS_KNOWN_WINDOWS, "headroom": [_week_row(80)]}
    rendered = _render(tmp_path, dict(COMMON, windows=windows))
    hero = rendered["cc-hero"]
    assert "NOTHING TO PUT DOWN — EVERY LIMIT HAS ROOM" in hero
    assert "EASE OFF" not in hero
