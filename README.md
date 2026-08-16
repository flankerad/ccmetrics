# ccmetrics

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
  <img src="docs/assets/logo-light.svg" width="560"
       alt="ccmetrics logo: four model-coloured bars, the tallest leaking clay-coloured token drops, beside the ccmetrics wordmark">
</picture>

**Will your plan last the week? ccmetrics races your burn against the clock, finds where Claude Code leaks tokens, and gives a paste-ready fix for each leak.**

![python](https://img.shields.io/badge/python-3.11%2B-3572A5)
![license](https://img.shields.io/badge/license-Apache--2.0-3fb950)
![CI](https://github.com/flankerad/ccmetrics/actions/workflows/ci.yml/badge.svg)
![deps](https://img.shields.io/badge/runtime%20deps-zero-8957e5)
![data](https://img.shields.io/badge/your%20data-never%20leaves%20your%20machine-8957e5)

<sub>[Install](#install) · [When you type, and where](#when-you-type-and-where) · [The views](#the-views) · [Commands](#all-the-commands) · [Detectors](#the-12-leak-detectors) · [Privacy](#privacy)</sub>

`/cost` shows what you spent. `ccmetrics` shows whether the week and the current 5-hour block last at today's pace, why the tokens go, and the exact `CLAUDE.md` line, `settings.json` fragment, or habit that stops each leak. Local: reads your session logs, sends nothing anywhere.

## Install

Python 3.11+. Zero runtime dependencies. Needs [Claude Code](https://claude.com/claude-code) — ccmetrics reads its session logs. Not on PyPI yet:

```bash
git clone https://github.com/flankerad/ccmetrics && cd ccmetrics
uv tool install .            # or: pipx install . / pip install -e .
ccmetrics                    # first run — wires everything below
```

First run: prints your summary, puts plan % in the status line, registers dashboard + widget at login, opens the dashboard once. Every later run only prints. Skip pieces with `--no-dash`, `--no-setup`, `--no-autostart`. Tk (widget) and other notes: [docs/install.md](docs/install.md).

Chat questions inside Claude Code need the bundled skill — one global copy, works in every repo:

```bash
mkdir -p ~/.claude/skills && cp -r .claude/skills/ccmetrics ~/.claude/skills/
```

## When you type, and where

After the first run the numbers come to you — status line, widget, dashboard at login. Typing covers four cases:

| You want to | Where you type | What you type |
|---|---|---|
| install or update | terminal | the install block above |
| ask in your own words | Claude Code chat (needs the skill) | *"will my plan last the week?"*, *"what's burning tokens?"*, *"open the dashboard"* |
| dig in right now | terminal | `ccmetrics`, `ccmetrics dash` — full list [below](#all-the-commands) |
| undo the wiring | terminal | `ccmetrics setup --revert` (status line back), `ccmetrics autostart --revert` (login services gone) |

The skill maps a chat question to the right command and answers from real numbers. On its first question of a session it re-wires anything missing; a status line another tool holds is backed up to `~/.claude/settings.json.bak-ccmetrics` first, undo command printed. Every command documented here or in the skill is parsed against the real CLI by [tests/test_skill_doc.py](tests/test_skill_doc.py) — a moved flag fails CI, not you.

## The views

**The summary** — plain `ccmetrics` in the terminal: spend floor, plan %, token mix, per-model table, top leaks with fixes.

![the ccmetrics terminal summary: spend floor, plan percentages, token mix, per-model table, and the top leak with its paste-ready fix](docs/assets/summary.svg)

**The status line** — plan numbers inside Claude Code: repo, branch, model, context, session cost, then 5-hour and weekly limits with a week bar. Segments with no data disappear.

![the ccmetrics status line inside Claude Code: repo, branch, model, context, session cost, 5-hour and weekly plan percentages with a week bar](docs/assets/statusline.png)

**The dashboard** — `ccmetrics dash`: browser, localhost, no build step.

![the ccmetrics dashboard, full page: the week fuse, limits left, the live session, recoverable leaks, this week's windows, the month, projects, and value absorbed](docs/assets/dash.png)

Panels: **week fuse** — flame at your spend, clock at where the week is; flame ahead = dry before reset, *Empty at* names the moment · **Limits left** — one meter per cap Anthropic reports, names which to ease off · **Live** — current session's burn rate, context, cache-hit, cost; a runaway session is flagged while it runs · **Recoverable** — leaks ranked by tokens back ÷ fix effort, copy button per fix · **week + month** as 5-hour windows coloured by how tight each ran · **projects** with each one's biggest leak · **value absorbed** — what the subscription covered at API prices.

**The widget** — `ccmetrics widget`: always-on-top fuse window. Drag anywhere; needs Tk.

![the ccmetrics widget: a small always-on-top panel showing the week's fuse, the burn flame, the current 5-hour block, and the burn rate](docs/assets/widget.png)

Minimize collapses it to one line; the face tracks the burn:

![the collapsed widget near-dry: the sweating face, 4% left, the fuse burnt almost to its end](docs/assets/widget-min-dry.png)

## All the commands

```bash
# read (the everyday ones)
ccmetrics             # this repo's summary: plan %, top leaks, paste-ready fixes
ccmetrics --global    # the same across every project
ccmetrics --all-leaks # every finding, not just the top few
ccmetrics dash        # dashboard in your browser
ccmetrics live        # the session running right now
ccmetrics widget      # always-on-top fuse window
ccmetrics constants   # every pricing constant with its source URL
ccmetrics otel        # local OTEL receiver (127.0.0.1:4318) for exact costs

# wire / unwire (first run does the --apply side for you)
ccmetrics setup       # status line: --apply, --check, --revert
ccmetrics autostart   # login services: --apply, --check, --revert
```

The two that write, and their undo:

| Command | Effect | Undo |
|---|---|---|
| `ccmetrics setup --apply` | plan % in your status line; an existing status line is saved to `~/.claude/settings.json.bak-ccmetrics` first | `ccmetrics setup --revert` |
| `ccmetrics autostart --apply` | two login services: one keeps the dash alive, one opens the widget | `ccmetrics autostart --revert` |

## The 12 leak detectors

Ranked by tokens saved ÷ fix difficulty, filled with your numbers. Every saving links its arithmetic: the hits summed, the threshold crossed, the pricing constant used.

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

`CLAUDE.md` lines are safe to paste as-is. `settings.json` suggestions are starting points — only you know which model choices and denials are deliberate.

## Why the numbers hold up

Claude Code logs every session to `~/.claude/projects/`, but the obvious fields are wrong: `usage.input_tokens` / `output_tokens` are unfinalized streaming placeholders, low by 17–174× ([claude-code#28197](https://github.com/anthropics/claude-code/issues/28197)). ccmetrics prices only the cache token fields × Anthropic's published multipliers and labels the result a floor; output cost is a separate range, never summed in. The optional OTEL receiver upgrades the floor to exact. Anything not derivable shows as unknown — wrong-but-confident is the failure mode this tool exists to avoid.

## Privacy

- **Local only.** No network egress of usage data, no account, no telemetry.
- **Metadata only.** Counts, byte sizes, timestamps, tool names, file paths, hashes — **never** prompt text, file contents, or tool-result bodies.
- **Read-only against your session logs.** Exactly two undoable writes elsewhere: the status-line entry in `~/.claude/settings.json` (backed up first) and the login-autostart entries — undo commands in the [table above](#all-the-commands).
- **One state file.** SQLite, capped under 100 MB, delete any time.

## Docs

- [Install notes](docs/install.md) — Tk for the widget, editable installs, login startup, first-run flags.
- [Plan limits](docs/plan-limits.md) — where the 5-hour and weekly percentages come from, what the status line does to your settings.

## Status

**v0.2.0 — usable.** Ingest, cost floor, all 12 detectors, console summary, dashboard, widget, optional OTEL exact costs work end to end. Next: PyPI release.

## License

Apache-2.0 — see [LICENSE](LICENSE).

<sub>Built with Claude Code — and pointed back at it.</sub>
