# ccmetrics

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo-light.svg" width="560"
       alt="ccmetrics logo: a spend sparkline whose hottest bar leaks orange token drops — cc in Claude orange, metrics in the theme text color">
</picture>

**Find where Claude Code burns your tokens — and get a paste-ready fix for each leak.**

![python](https://img.shields.io/badge/python-3.11%2B-3572A5)
![license](https://img.shields.io/badge/license-Apache--2.0-3fb950)
![CI](https://github.com/flankerad/ccmetrics/actions/workflows/ci.yml/badge.svg)
![deps](https://img.shields.io/badge/runtime%20deps-zero-8957e5)
![data](https://img.shields.io/badge/your%20data-never%20leaves%20your%20machine-8957e5)

<sub>[Why](#why) · [Use](#use) · [Install](#install) · [Detectors](#the-12-leak-detectors) · [Privacy](#privacy--the-hard-rules) · [Numbers](#numbers-you-can-defend) · [Status](#status)</sub>

`/cost` tells you how much you spent. `ccmetrics` tells you *why*, tracks it over time, and hands you the exact `CLAUDE.md` line, `settings.json` fragment, or habit that stops the bleed. Local, private, read-only.

```
┌ LIVE ── burn 12%/hr │ ctx 61% │ cache-hit 94% │ session at least $0.83 ┐
SPEND 30d  ▂▃▅▇▅▆█  at least $41.20 + $6–9 est. output    TOKEN MIX ██████░░
TOP LEAKS                              SAVES        FIX
1 Cold start after a break             ~1.2M tok    [copy] CLAUDE.md line
2 Compaction tax (9 sessions)          ~640K tok    [copy] /compact habit
3 Premium model on small turns         ~410K tok    [copy] settings.json
▸ per-project ▸ per-session ▸ per-turn timeline
```
<sub>illustration, not a screenshot — the real thing is `ccmetrics` in your terminal and `ccmetrics dash` in your browser</sub>

## Why

Claude Code writes a detailed JSONL log of every session to `~/.claude/projects/`. Almost nobody reads it — and the tools that do get the numbers wrong:

- **The obvious token fields lie.** `usage.input_tokens` / `output_tokens` are streaming placeholders that undercount by 100–174×. Most dashboards sum them anyway.
- **The cache fields are accurate.** Every dollar figure here is built from cache reads/writes × Anthropic's published multipliers (1.25× / 2× / 0.1×), labelled as a **floor** — never a guess dressed up as a fact.
- **Nobody closes the loop.** Existing tools show charts. None detect *leak patterns* and attach a fix you can paste.

## Use

```bash
ccmetrics        # inside a repo → that repo's summary: top leaks + paste-ready fixes
ccmetrics dash   # anywhere → global dashboard in your browser, per-project drill-down
ccmetrics widget # optional → small always-on-top window with the week fuse (needs the dash running)
ccmetrics otel   # optional → local OTEL receiver (127.0.0.1:4318) for exact costs
ccmetrics setup --revert  # the status line wires itself on first run; this undoes it
```

### Your first run

Type `ccmetrics` once and it sets itself up around you. It prints your summary, wires its own status line into Claude Code so your plan usage shows there, then opens the dashboard in your browser — and the small always-on-top window too, if your Python has Tk. Press ctrl-c when you have seen enough. It only does this the first time; every run after that just prints.

Not the welcome you wanted? `ccmetrics --no-dash` skips the dashboard, `ccmetrics --no-setup` skips the status line, and the environment variables `CCMETRICS_NO_DASH=1` and `CCMETRICS_NO_SETUP=1` do the same for good. None of it happens when the output is piped, when you pass `--json`, or when Claude Code isn't installed — in that last case ccmetrics simply tells you it found no sessions and stops.

### The widget needs Tk

`ccmetrics widget` draws with Tk, the graphics toolkit Python normally bundles.
Nothing installs it for you, and several Python builds leave it out — including
the interpreter `uv` downloads for itself. Everything else in ccmetrics runs
without it.

| Platform | What you need |
|---|---|
| Windows | nothing — the python.org installer includes Tk |
| macOS (Apple's own python3) | nothing |
| macOS (Homebrew) | `brew install python-tk` |
| Debian/Ubuntu | `sudo apt install python3-tk` |
| Fedora | `sudo dnf install python3-tkinter` |

If `uv tool install` picked a Python without it, point the install at one that
has it: `uv tool install --force --python $(which python3) .`

Check yours with `python3 -c "import tkinter"` — silence means it works.

The dashboard glance view, zero clicks: live session tiles (burn rate, context %, cache-hit, spend — a runaway session gets flagged *while it's running*), 30-day spend trend, token mix, top leaks ranked by `tokens saved ÷ effort` each with a copy button, and a per-project table.

## Install

Python 3.11+. Zero runtime dependencies — stdlib SQLite, stdlib HTTP server, one static HTML page.

```bash
# today (from a clone — not on PyPI yet):
git clone https://github.com/flankerad/ccmetrics && cd ccmetrics
uv tool install .            # or: pipx install . / pip install -e .

# after the PyPI release:
uv tool install ccmetrics    # or: pipx install ccmetrics
```

One install covers every project on the machine: it reads all of `~/.claude/projects/`, keeps per-project data separate, and rolls it up globally in the dash.

### Plan limits (optional)

How much of your Pro/Max plan is gone is **not** in any file on your machine — it lives on Anthropic's servers. So ccmetrics never estimates it. There is exactly one honest local source: Claude Code hands its status-line command its own session JSON, and for subscribers that JSON carries `rate_limits` — the same percentages `/usage` shows.

Your status line becomes `ccmetrics > main | ✳ Opus :: 48k/200k (24%) | 5h 31% | wk 62%`, coloured green through red by how much you have spent, while `ccmetrics dash` grows a **PLAN** card and `ccmetrics` prints a PLAN line. Only the percentage, the reset time and the session id are stored (90 days, metadata as always); readings older than 6 hours are labelled stale rather than shown as current. Undo the wiring and the card and the line simply stop appearing.

#### It turns itself on

Claude Code only runs one status-line command, so wiring ccmetrics in by hand would mean editing JSON. The first time you run `ccmetrics`, it does that for you and says so:

```
ccmetrics wired its status line into ~/.claude/settings.json — your plan usage (5h and weekly %) now shows there.
undo any time: ccmetrics setup --revert
```

It tries this exactly once, ever. It stays out of the way when nobody is watching: piped output, `--json`, and the status-line hook itself never trigger it. `CCMETRICS_NO_SETUP=1` or `ccmetrics --no-setup` skips it entirely, and `ccmetrics setup --check` tells you where things stand, including when a plan % was last seen. Skipped it and changed your mind? `ccmetrics setup --apply` wires it by hand.

What the wiring does: it opens `~/.claude/settings.json` (create `--settings <path>` to point at a different file, e.g. a project-level settings.json), saves a backup next to it (`settings.json.bak-ccmetrics`), and sets the status line to `ccmetrics statusline`. If you already had a status line command, it wraps yours instead of replacing it, so your status line still looks the same — ccmetrics just also records your plan %. It's safe to run more than once: if it's already wired up, it says so and changes nothing.

Changed your mind? `ccmetrics setup --revert` puts your settings back the way they were — no need for the backup file, it reads its own state back out of the settings file.

If `ccmetrics` isn't on your PATH, the wiring writes the full path to whichever `ccmetrics` you ran it with, so the status line keeps working either way. `--check` will tell you loudly if that ever stops being true.

## The 12 leak detectors

Each ships a pre-written fix template filled from your own numbers:

| # | Leak | Fix shape |
|---|------|-----------|
| 1 | Cache-miss on idle gaps (resumed past the cache window) | `CLAUDE.md` line |
| 2 | Compaction tax (compacting too late, too often) | `/compact` habit |
| 3 | Context bloat (oversized always-on instructions) | trim list |
| 4 | MCP tool-call overhead (measured per call, not folklore) | config change |
| 5 | Premium model on trivially small turns | `/model` habit (`settings.json` routing as a reviewed option) |
| 6 | Repeated identical tool calls (same input, same session) | `CLAUDE.md` line |
| 7 | Oversized tool results flowing into context | scoping tip |
| 8 | Sidechain/agent overspend | agent-sizing tip |
| 9 | Retry/error loops burning turns (hooks, denials) | review — keep or allowlist, your call |
| 10 | Stale-session sprawl (many half-dead sessions) | hygiene tip |
| 11 | Runaway live session (burn ≫ your own p90) | live warning |
| 12 | File re-read waste (same file read 3×+, unchanged) | `CLAUDE.md` read-discipline line |

Every saving shown links its arithmetic: the detector hits it sums, the threshold it crossed, and the pricing constant (with source URL) it used.

One honesty rule: the `CLAUDE.md` lines and habits are safe to paste as-is; the `settings.json` suggestions are starting points — read them first, because only you know which model choices and permission denials are deliberate.

## Privacy — the hard rules

- **Local only.** No network egress of usage data, no account, no cloud, no telemetry.
- **Metadata only.** Stores counts, byte sizes, timestamps, tool names, file paths, and hashes — **never** prompt text, file contents, or tool-result bodies.
- **Read-only against `~/.claude`.** It cannot damage your Claude Code install or sessions.
- **Proposes, never applies.** Every leak fix is yours to read and paste — ccmetrics edits none of your files. One exception, named here so it is never a surprise: on its very first run it adds its own status-line command to `~/.claude/settings.json`, which is how your plan percentages reach you without you doing anything. It copies that file first, keeps any status line you already had, and `ccmetrics setup --revert` puts it back. `CCMETRICS_NO_SETUP=1 ccmetrics` — or `ccmetrics --no-setup` — leaves the file untouched. Nothing else on your disk is ever written to.
- One state file (SQLite, capped under 100 MB); delete it any time — nothing of yours is lost.

## Numbers you can defend

- Dollar figures are a **floor** computed from accurate cache fields × published Anthropic multipliers. Output cost appears as a clearly-labelled byte-derived *range*, never silently summed in.
- Cost confidence is a visible UI state: approximate (JSONL-only) vs exact (optional OTEL upgrade).
- Every pricing constant and detector threshold lives in one versioned lookup file, each entry with a source URL.
- Plan-limit percentages are shown only when Claude Code itself reports them through the status-line hook, always with their age — never derived, never guessed.
- Anything not derivable is shown as unknown — wrong-but-confident is the failure mode this tool exists to avoid.

## Status

**v0.2.0 — usable.** Ingest, cost floor, all 12 detectors, console summary, localhost dashboard, and optional OTEL exact costs work end-to-end (76-test suite green; cold ingest of a 522 MB corpus in ~5 s). Next: PyPI release.

## License

Apache-2.0 — see [LICENSE](LICENSE).
