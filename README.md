# ccmetrics

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo-light.svg" width="520"
       alt="ccmetrics logo: four model-coloured bars, the tallest leaking clay-coloured token drops, beside the ccmetrics wordmark">
</picture>

**Find where Claude Code burns your tokens — and get a paste-ready fix for each leak.**

![python](https://img.shields.io/badge/python-3.11%2B-3572A5)
![license](https://img.shields.io/badge/license-Apache--2.0-3fb950)
![CI](https://github.com/flankerad/ccmetrics/actions/workflows/ci.yml/badge.svg)
![deps](https://img.shields.io/badge/runtime%20deps-zero-8957e5)
![data](https://img.shields.io/badge/your%20data-never%20leaves%20your%20machine-8957e5)

<sub>[Install](#install) · [Use](#use) · [What you get](#what-you-get) · [Detectors](#the-12-leak-detectors) · [Privacy](#privacy) · [Docs](#docs)</sub>

`/cost` tells you how much you spent. `ccmetrics` tells you *why*, tracks it over time, and hands you the exact `CLAUDE.md` line, `settings.json` fragment, or habit that stops the bleed. Local, private, read-only.

```
ccmetrics · your-project · last 30 days

SPEND   at least $41.20
        + est. output $6.10–$9.15   (a range, never added to the number above)

PLAN    wk 62% (resets Mon 17:30) · 5h 31% (resets Sat 22:20)
        your plan, straight from Anthropic via Claude Code's status line

TOKENS  ███████████████████████▒  read 61.4M █ · write-5m 1.1M ▓ · write-1h 903K ▒
        cache-hit 94% · 612 turns · 9 sessions · 2 compactions

TOP LEAKS (ranked by tokens saved vs how hard the fix is)
  1. Premium model on small turns       ~   1.2M tok    $12.83  effort:paste
     WHAT:  Send the quick turns to a cheaper model.
     DO:    /model claude-haiku-4-5 — switch back when the work gets hard.
     WATCH: { "model": "claude-haiku-4-5" } in settings.json routes EVERY turn
            there, not just the small ones. Review before pasting that.

run `ccmetrics --all-leaks` for every finding, `ccmetrics constants` for sources
```
<sub>the shape of real `ccmetrics` output, with example numbers.</sub>

## Install

Python 3.11+. Zero runtime dependencies.

```bash
git clone https://github.com/flankerad/ccmetrics && cd ccmetrics
uv tool install .            # or: pipx install . / pip install -e .
```

Then run `ccmetrics` once. It prints your summary, wires its own status line into Claude Code so your plan usage shows there, opens the dashboard, and registers both to start at login. It does that on the first run only — every run after that just prints.

Skip any of it with `--no-dash`, `--no-setup`, or `--no-autostart`. Full notes, including the Tk requirement for the widget: [docs/install.md](docs/install.md).

## Use

```bash
ccmetrics             # this repo's summary: top leaks + paste-ready fixes
ccmetrics dash        # global dashboard in your browser, per-project drill-down
ccmetrics live        # burn rate for the session running right now
ccmetrics widget      # small always-on-top window with the week's fuse
ccmetrics otel        # local OTEL receiver (127.0.0.1:4318) for exact costs
ccmetrics constants   # every pricing constant with its source URL
ccmetrics setup       # status line: --apply, --check, --revert
ccmetrics autostart   # login startup: --apply, --check, --revert
```

## What you get

- **A dollar floor you can defend.** Every figure comes from the accurate cache fields × Anthropic's published multipliers, labelled a floor. Output cost appears as a separate range, never silently summed in.
- **12 leak detectors, each with a fix.** Ranked by tokens saved ÷ how hard the fix is, filled in with your own numbers.
- **A dashboard at a glance.** Live session tiles (burn rate, context %, cache-hit, spend — a runaway session is flagged *while it runs*), a 30-day spend trend, the token mix, top leaks with a copy button, and a per-project table.
- **Your plan usage in the status line.** The 5-hour and weekly percentages Anthropic reports, plus an eight-cell bar for the week. See [docs/plan-limits.md](docs/plan-limits.md).
- **An always-on-top widget.** The week burns down as a fuse, with a flame at your burn point and a clock at where the week actually is. It collapses to a single line.
- **Exact costs when you want them.** The optional OTEL receiver upgrades the floor to exact, and the dashboard tells you which one you are looking at.

## Why

Claude Code writes a detailed JSONL log of every session to `~/.claude/projects/`. Almost nobody reads it, and the tools that do get the numbers wrong:

- **The obvious token fields lie.** `usage.input_tokens` / `output_tokens` are streaming placeholders that undercount by 100–174×. Most dashboards sum them anyway.
- **The cache fields are accurate.** Every dollar here is built from them, never from a guess dressed up as a fact.
- **Nobody closes the loop.** Other tools show charts. None detect leak *patterns* and attach a fix you can paste.

## The 12 leak detectors

| # | Leak | Fix shape |
|---|------|-----------|
| 1 | Cache-miss on idle gaps (resumed past the cache window) | `CLAUDE.md` line |
| 2 | Compaction tax (compacting too late, too often) | `/compact` habit |
| 3 | Context bloat (oversized always-on instructions) | trim list |
| 4 | MCP tool-call overhead (measured per call, not folklore) | config change |
| 5 | Premium model on trivially small turns | `/model` habit |
| 6 | Repeated identical tool calls (same input, same session) | `CLAUDE.md` line |
| 7 | Oversized tool results flowing into context | scoping tip |
| 8 | Sidechain/agent overspend | agent-sizing tip |
| 9 | Retry/error loops burning turns (hooks, denials) | review — your call |
| 10 | Stale-session sprawl (many half-dead sessions) | hygiene tip |
| 11 | Runaway live session (burn ≫ your own p90) | live warning |
| 12 | File re-read waste (same file read 3×+, unchanged) | read-discipline line |

Every saving links its arithmetic: the hits it sums, the threshold it crossed, and the pricing constant it used. The `CLAUDE.md` lines are safe to paste as-is. The `settings.json` suggestions are starting points — only you know which model choices and permission denials are deliberate.

## Privacy

- **Local only.** No network egress of usage data, no account, no cloud, no telemetry.
- **Metadata only.** Counts, byte sizes, timestamps, tool names, file paths, hashes — **never** prompt text, file contents, or tool-result bodies.
- **Read-only against `~/.claude`.** It cannot damage your Claude Code install or sessions.
- **Proposes, never applies.** ccmetrics edits none of your files. The one exception, named so it is never a surprise: on first run it adds its own status-line command to `~/.claude/settings.json`, backing up whatever was there. `ccmetrics setup --revert` puts it back, and `--no-setup` skips it.
- **One state file.** SQLite, capped under 100 MB, delete it any time.
- **Unknown stays unknown.** Anything not derivable is shown as unknown. Wrong-but-confident is the failure mode this tool exists to avoid.

## Docs

- [Install notes](docs/install.md) — Tk for the widget, editable installs, login startup, first-run flags.
- [Plan limits](docs/plan-limits.md) — where the 5-hour and weekly percentages come from, and what the status line does to your settings.

## Status

**v0.2.0 — usable.** Ingest, cost floor, all 12 detectors, console summary, dashboard, widget, and optional OTEL exact costs work end to end. Next: PyPI release.

## License

Apache-2.0 — see [LICENSE](LICENSE).
