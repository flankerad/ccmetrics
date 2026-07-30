# ccmetrics

**Find where Claude Code burns your tokens — and get a paste-ready fix for each leak.**

`/cost` tells you how much you spent. `ccmetrics` tells you *why*, tracks it over time, and hands you the exact `CLAUDE.md` line, `settings.json` fragment, or habit that stops the bleed. Local, private, read-only.

> **Status: v0.2.0 — usable.** Ingest, cost floor, all 12 detectors, console summary, and the localhost dashboard work end-to-end, plus optional OTEL exact costs (70-test suite green). Packaging to PyPI is next; until then: `pip install -e .` from a clone.

```
┌ LIVE ── burn 12%/hr │ ctx 61% │ cache-hit 94% │ session $0.83 (floor) ┐
SPEND 30d  ▂▃▅▇▅▆█   $41.20 floor + $6–9 est. output     TOKEN MIX ██████░░
TOP LEAKS                              SAVES        FIX
1 Cache-miss on idle gaps              ~1.2M tok    [copy] CLAUDE.md line
2 Compaction tax (9 sessions)          ~640K tok    [copy] /compact habit
3 Premium model on small turns         ~410K tok    [copy] settings.json
▸ per-project ▸ per-session ▸ per-turn timeline
```

## Why this exists

Claude Code writes a detailed JSONL log of every session to `~/.claude/projects/`. Almost nobody reads it — and the tools that do read it get the numbers wrong:

- **The obvious token fields lie.** `usage.input_tokens` / `output_tokens` are streaming placeholders that undercount by 100–174×. Most usage dashboards sum them anyway.
- **The cache fields are accurate.** `ccmetrics` builds every dollar figure from cache reads/writes and Anthropic's published multipliers (1.25× / 2× / 0.1×), labelled as a **floor** — never a guess dressed up as a fact.
- **Nobody closes the loop.** Existing tools show you charts. None of them detect *leak patterns* and attach a fix you can paste.

## What it does

Two surfaces, one store:

```bash
ccmetrics        # inside a repo → that repo's console summary: top leaks + fixes
ccmetrics dash   # anywhere → global dashboard in your browser, per-project drill-down
```

**The dashboard glance view** (zero clicks needed):

- **Live session tiles** — burn rate, context-window %, cache-hit ratio, spend so far. Updates every turn. A runaway session gets flagged *while it's still running*.
- **30-day spend trend** — floor + a labelled output estimate band. Never a fake-precise total.
- **Token mix** — where the volume actually goes (spoiler: cache reads dominate).
- **Top leaks, ranked by `tokens saved ÷ effort`** — each with a copy button holding the actual fix.
- **Per-project table** — spend, trend delta, and each project's worst leak; click through to that repo's own view.

**12 leak detectors**, each with a pre-written fix template filled from your own numbers:

| # | Leak | Fix shape |
|---|------|-----------|
| 1 | Cache-miss on idle gaps (resumed past the cache window) | `CLAUDE.md` line |
| 2 | Compaction tax (compacting too late, too often) | `/compact` habit |
| 3 | Context bloat (oversized always-on instructions) | trim list |
| 4 | MCP tool-call overhead (measured per call, not folklore) | config change |
| 5 | Premium model on trivially small turns | `settings.json` routing |
| 6 | Repeated identical tool calls (same input, same session) | `CLAUDE.md` line |
| 7 | Oversized tool results flowing into context | scoping tip |
| 8 | Sidechain/agent overspend | agent-sizing tip |
| 9 | Retry/error loops burning turns | habit |
| 10 | Stale-session sprawl (many half-dead sessions) | hygiene tip |
| 11 | Runaway live session (burn ≫ your own p90) | live warning |
| 12 | File re-read waste (same file read 3×+, unchanged) | `CLAUDE.md` read-discipline line |

Every saving shown links its arithmetic: the detector hits it sums, the threshold it crossed, and the pricing constant (with source URL) it used. No unsourced claims.

## Install

Requires Python 3.11+. Zero runtime dependencies — stdlib SQLite and stdlib HTTP server, one static HTML page.

```bash
uv tool install ccmetrics    # or: pipx install ccmetrics
```

One install covers every project on the machine: it reads all of `~/.claude/projects/`, keeps per-project data separate, and rolls it up globally in the dash.

## Privacy — the hard rules

- **Local only.** No network egress of usage data, no account, no cloud, no telemetry.
- **Metadata only.** Stores counts, byte sizes, timestamps, tool names, file paths, and hashes — **never** prompt text, file contents, or tool-result bodies. Your transcripts stay where they are, as the only copy.
- **Read-only against `~/.claude`.** It cannot damage your Claude Code install or sessions.
- **Proposes, never applies.** No file of yours is ever edited. You read the fix, you paste it.
- One state file (SQLite, capped under 100 MB), delete it any time — nothing of yours is lost.

## How it compares

| | usage charts | trends over time | leak detection | paste-ready fixes | trusts the right fields |
|---|---|---|---|---|---|
| `ccusage` | ✅ | ➖ | ❌ | ❌ | ❌ |
| `sniffly` | ✅ transcript archaeology | ➖ | ❌ | ❌ | ➖ |
| `ccflare` | ✅ via proxy | ✅ | ❌ | ❌ | ✅ (intercepts traffic) |
| built-in `/usage` | ✅ point-in-time | ❌ | ➖ two flags | ❌ | ✅ |
| **ccmetrics** | ✅ | ✅ | ✅ 12 detectors | ✅ | ✅ no proxy needed |

## Numbers you can defend

- Dollar figures are a **floor** computed from accurate cache fields × published Anthropic multipliers. Output cost appears as a clearly-labelled byte-derived *range*, never silently summed in.
- Cost confidence is a visible UI state: approximate (JSONL-only) vs exact (optional OTEL upgrade).
- Every pricing constant and detector threshold lives in one versioned lookup file, each entry with a source URL.
- Wrong-but-confident is the failure mode this tool is built to avoid: anything not derivable is shown as unknown.

## Roadmap

- [x] Research: JSONL schema, pricing, prior art (verified against a 540 MB / 1,536-session corpus)
- [x] Spec: 7 requirement areas, 12 detectors, all open questions closed
- [x] Dashboard mockup approved
- [x] v0.1.0: ingest → SQLite → 12 detectors → console + dash + live tiles (56 tests green; cold ingest of 522 MB in ~5 s)
- [ ] PyPI release (`uv tool install ccmetrics`)
- [x] Optional OTEL ingestion for exact costs (v0.2.0 — `ccmetrics otel`, receiver on 127.0.0.1:4318)

## License

MIT (planned with v1 release).
