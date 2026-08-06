"""dash/static/index.html: source-presence checks only, no JS execution
(PLAN-dash-v2 7 / 8.5). Fixtures + env-overridden paths only -- never the
user's real ~/.claude.json or DB.

Zero external URLs is the load-bearing check (decision 13): the page is
supposed to be a single offline artifact once Inter is embedded. That
embedding is itself ~470KB of base64 text with arbitrary character runs in
it, so the URL check anchors on a real scheme:// pattern rather than any
substring -- a coincidental escape-looking run inside the font blob is not a
leaked bug, and must not trip this test.
"""

from __future__ import annotations

import re
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from ccmetrics.dash import server as dash_server

INDEX = Path(__file__).resolve().parent.parent / "ccmetrics" / "dash" / "static" / "index.html"

_URL_PATTERN = r"(?:https?|ws|wss|ftp)://\S+"
_URL_RE = re.compile(_URL_PATTERN)


@pytest.fixture(scope="module")
def page_text():
    return INDEX.read_text(encoding="utf-8")


def _free_port():
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
        yield "http://127.0.0.1:" + str(port), httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_page_parses_as_html(page_text):
    assert page_text.startswith("<!DOCTYPE html>")
    assert page_text.rstrip().endswith("</html>")
    pairs = [("<html", "</html>"), ("<head>", "</head>"), ("<body>", "</body>")]
    for open_tag, close_tag in pairs:
        assert page_text.count(open_tag) == 1
        assert page_text.count(close_tag) == 1
    assert page_text.count("<script>") == page_text.count("</script>")


def test_no_external_urls_outside_the_font_data_uri(page_text):
    for m in _URL_RE.finditer(page_text):
        hit = m.group(0)
        assert hit.startswith("data:"), hit[:60]


def test_font_face_embedded_with_full_weight_range(page_text):
    assert "@font-face" not in page_text
    assert "ui-monospace" in page_text


def test_both_palettes_defined(page_text):
    assert ":root{" in page_text
    assert "data-palette" not in page_text
    assert "--bg:#191714" in page_text
    assert "--opus:#7594c9" in page_text
    assert page_text.count("--l0:#2b2a26") == 1


def test_caps_unknown_sentences_present_in_source(page_text):
    """12-pixel-stories: the caps-unknown ribbon is deleted -- its sentences
    now live in heroFuse's verdict copy directly."""
    assert "caps_known" in page_text
    assert "Caps are unknown until Claude Code" in page_text


def test_hero_verdict_covers_absolute_exhaustion(page_text):
    """The verdict must not rely on clock-relative pace alone -- a nearly
    spent week is a crisis regardless of whether usage is ahead of or
    behind the clock (source-presence guard; logic itself has no unit
    under test since it's inline JS)."""
    assert "The week is almost gone." in page_text
    assert "Getting tight" in page_text
    assert "nearlyGone" in page_text
    assert "gettingTight" in page_text


def test_hero_verdict_single_digits_left_reads_as_urgent(page_text):
    """PLAN-cap-and-chrome Part 2 #3: at 9% left the old wording ("Getting
    tight -- most of the week is spent") was too mild for a tenth of a week
    remaining. Single-digit percentages now get their own, more urgent tier
    between "getting tight" (<=15%) and "almost gone" (<=5%), and the
    already-strongest "almost gone" wording is unchanged (source-presence
    guard; the branching itself is inline JS)."""
    assert "singleDigits" in page_text
    assert "pctLeft < 10" in page_text
    assert "Single digits left" in page_text
    assert "The week is almost gone." in page_text  # unchanged, still the strongest


def test_hero_face_is_an_emoji_scale_including_the_hot_end(page_text):
    """PLAN-dash-ui-fixes 1: the kaomoji register became real emoji, calm ->
    alarmed; "🥵" is the explicitly requested hot-end glyph."""
    assert "🥵" in page_text
    assert "😐" in page_text


def test_punch_list_2_strings_present(page_text):
    """PLAN-dash-v2 6.4e: nine human-visible strings the mock carries that a
    prior pass dropped or renamed. Regression guard so they cannot silently
    vanish again -- source presence only, exact wording where the mock's own
    wording is literal (not derived from live data)."""
    # item 1 (week-grid morning/afternoon/evening/late column headers) was
    # the slot-label row -- 10-pixel-week-month deletes it, the mock has none.
    # items 2-5, 8 (magnified sub-panel, quota legend, writes sentence,
    # projects column-header row, "four tallest days" prose) are dropped by
    # 11-pixel-live-projects-value's VALUE ABSORBED/PROJECTS redesign.
    assert "everything else" in page_text  # item 6
    assert "cc-theme-btn" not in page_text  # item 7: theme toggle removed with the palette system
    assert "FOUR TALLEST DAYS = " in page_text  # item 8, new wording


def test_punch_list_3_correctness_and_strings(page_text):
    """PLAN-dash-v2 6.4g: source-presence guards for items 3-7 (items 1-2 are
    logic bugs, covered by tests/test_windows.py's falsify_stale_caps and
    early_hours tests instead)."""
    # item 3: no duplicated unit -- the old bug concatenated "/hr" onto the
    # value AND the label already said "per hour"
    # 07-pixel-hero: the LIVE strip owns block rate now, so the RATE stat
    # drops this line entirely (its own tests pin the new home later).
    assert "of a block per hour" not in page_text
    assert 'fmtPct(hero.pct_of_block_per_hour) + "/hr"' not in page_text
    assert "Holds" in page_text  # runs_out_at after resets_at: the week holds

    # item 4: cwd basename/~-abbreviation, not the raw sanitized store key
    assert "function homeAbbrev" in page_text
    assert "function baseName" in page_text
    assert "baseName(r.cwd)" in page_text

    # item 5: the short one-sentence summary, fix_text kept behind Copy fix
    assert "function oneSentence" in page_text
    assert "oneSentence(item.help)" in page_text
    # fix_text still exists (behind Copy fix, via data-fix), just not as the
    # row body -- the row body div now reads from oneSentence(), not fix_text
    # the clipped body still comes from oneSentence(); clicking it swaps in the
    # detector's whole help text (findOpenHelp keeps it open across re-renders)
    assert "oneSentence(item.help)) + " + chr(34) + "</div>" + chr(34) in page_text
    assert "findOpenHelp" in page_text and 'cc-help-' in page_text
    assert 'data-fix=' in page_text and 'esc(item.fix_text || "")' in page_text

    # item 6: absurd percentages get worded, not printed -- the whole
    # delta_pct/leak-arrow mechanism this guarded is deleted by
    # 11-pixel-live-projects-value (mock's PROJECTS rows carry no delta).

    # item 7 (week grid hover wiring) is gone: 10-pixel-week-month's mock has
    # no hover readouts at all, in THIS WEEK or MONTH.


def test_empty_store_page_parses_no_exception(conn, cc_env, running_server):
    base, _ = running_server
    with urllib.request.urlopen(base + "/", timeout=5) as resp:
        assert resp.status == 200
        assert "text/html" in resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8")
    assert body.startswith("<!DOCTYPE html>")
    assert body.rstrip().endswith("</html>")
    assert "__INTER_WOFF2_BASE64__" not in body


def test_month_strip_is_hoverless_and_colours_straight_from_pct_of_week(page_text):
    """10-pixel-week-month: naming leftover from before hover_implementation.md
    (D5-D8) added a hover panel to every strip -- this test still only pins
    the DATA-DRIVEN fill colour a cell shows at rest, which the hover panel
    never changes the source of, only overlays with a piano-key press --
    each cell's fill is coloured from pct_of_week when the weekly cap is
    known, falling back to a flat two-step token-relative neutral (never a
    colour ramp) only when caps are genuinely unknown. D1 bug-1 follow-up:
    pct_of_week is a share of a WEEK, not a 5-hour block, so it grades on
    the restored WEEK_LVL_EDGES ladder (pxLvlWeek) rather than pxLvl's
    45/75/95 5-hour-cap edges, which parked every real cell in the lowest
    band forever."""
    assert "PX_LVL[pxLvlWeek(c.pct_of_week)]" in page_text
    assert 'bg = frac > 0.5 ? "var(--line)" : "var(--l0)";' in page_text
    assert "cc-hz-cell" not in page_text
    assert "state.hz" not in page_text
    assert "FILL IS RELATIVE TO THE HEAVIEST WINDOW IN RANGE" in page_text


def test_week_grid_cell_fill_falls_back_to_pct_of_week_when_pct_of_cap_is_null(page_text):
    """D1 bug-1 fix: pct_of_cap needs the 5-hour SESSION cap specifically and
    is null on every real week cell once only a WEEKLY cap has been
    estimated, even though caps_known is true -- so the 7-block meter must
    fill AND colour from pct_of_week in that case instead of drawing empty.
    D1 bug-1 follow-up: pct_of_week is a share of a WEEK, so the fallback
    goes through weekFillPct/pxLvlWeek (the restored WEEK_LVL_EDGES ladder),
    not pxLvl's 5-hour-cap edges, or every real cell reads as one lit
    segment in the lowest band regardless of how heavy it actually was.
    caps-unknown windows still fall back to a token-relative fill against
    var(--line), no colour ramp."""
    assert "fillPct = weekFillPct(c.pct_of_week);" in page_text
    assert "on = PX_LVL[pxLvlWeek(c.pct_of_week)];" in page_text
    assert "TOKENS SHOWN, NOT FILL — CAP UNKNOWN" in page_text


def test_fuse_day_row_derives_from_resets_at_and_tick_clearance_is_gone(page_text):
    """The old midnight-tick day strip (and its FUSE_TICK_CLEARANCE crowding
    guard) is replaced by 7 equal columns keyed off the reset instant; the
    clock rule is its own marker now, with no accent-coloured hairline
    left to crowd (pixel redesign supersedes PLAN-cap-and-chrome Part 2 #1)."""
    assert "(startDay + di) % 7" in page_text
    assert "FUSE_TICK_CLEARANCE" not in page_text
    assert "background:var(--bg);box-shadow:2px 0 0 0 var(--ink);z-index:6" in page_text


def test_clock_caption_is_gone_rule_alone_marks_it(page_text):
    """PLAN-cap-and-chrome Part 2 #1: the "the clock is here" caption and its
    arrow are removed entirely -- the vertical rule (now a two-tone marker
    hanging off the pixel clock) is the only marker left."""
    assert "the clock is here →" not in page_text
    assert "← the clock is here" not in page_text
    assert "clockCaptionText" not in page_text
    assert "clockCaptionCss" not in page_text
    assert "clockCaptionRight" not in page_text
    # the rule itself is untouched in spirit: still there, now two-tone
    assert "background:var(--bg);box-shadow:2px 0 0 0 var(--ink);z-index:6" in page_text


def test_hover_keyframes_are_stepped_never_eased(page_text):
    """hover_implementation.md #1: the piano-key cell lift and the panel's
    snap-in must both arrive in discrete frames -- steps(2,end)/steps(3,end)
    -- never ease/linear, or the motion reads as web-smooth instead of
    mechanical. Pins the keyframe declarations and every usage site."""
    assert "@keyframes cckey{0%{transform:translateY(0)}100%{transform:translateY(-3px)}}" in page_text
    assert "@keyframes cctip{" in page_text
    cctip_kf = page_text[page_text.index("@keyframes cctip{"):]
    cctip_kf = cctip_kf[: cctip_kf.index("}}") + 2]
    assert "translateX" not in cctip_kf  # cctip is opacity/translateY only, never horizontal
    assert "ease" not in cctip_kf and "linear" not in cctip_kf
    assert "cckey 0.09s steps(2,end) forwards" in page_text
    assert "cctip 0.16s steps(3,end) forwards" in page_text


def test_hover_edge_clamping_flushes_first_and_last_cells(page_text):
    """hover_implementation.md #4: edge clamping is derived from the cell's
    own index (edgeFor), never a hardcoded left:50% -- first/last N cells
    pin flush instead of centring off-screen."""
    assert "function edgeFor(i, len, n)" in page_text
    assert "left:0;right:auto" in page_text
    assert "left:auto;right:0" in page_text
    assert "left:50%;transform:translateX(-50%)" in page_text


def test_hover_panels_use_one_shared_key_and_delegated_wiring(page_text):
    """hover_implementation.md #2: a single state.key drives every panel on
    the page (week/month/dollars ids never collide), and the listeners are
    delegated on the host -- not re-attached per cell per render, which is
    what let a panel stick open after its own DOM node got replaced."""
    assert "key: null" in page_text
    assert "state.key ===" in page_text
    assert "function wireHoverDelegate(host, rerender)" in page_text
    assert "cc-hz-cell" not in page_text
    assert "state.hz" not in page_text
