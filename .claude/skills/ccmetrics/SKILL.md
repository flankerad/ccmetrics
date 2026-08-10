---
name: ccmetrics
description: Answer questions about Claude Code plan pacing, token usage, costs, and leaks by running the local ccmetrics CLI. Use when the user asks "will my plan last", "how much have I used", "what's burning tokens", "any leaks", or wants a usage summary, live session status, or the dashboard/widget opened.
---

# ccmetrics skill

ccmetrics is this repo's tool: a read-only leak meter for Claude Code sessions. It is already installed as the `ccmetrics` command. Run it; never re-implement its analysis.

## Pick the command from the question

| User asks | Run |
|---|---|
| Will my plan last / pacing / plan % | `ccmetrics` and read the PLAN line (wk %, 5h %, reset times) — plan % is NOT in `--json` |
| Usage summary for this repo | `ccmetrics` (30-day summary, human output) |
| Usage across all projects | `ccmetrics --global` |
| What's leaking / wasting tokens | `ccmetrics detectors` (add `--evidence` for proof lines, `--all-leaks` for the full list) |
| Current session, live | `ccmetrics live` |
| Open the dashboard | `ccmetrics dash` (127.0.0.1:7433, run in background) |
| Open the floating widget | `ccmetrics widget` (run in background) |
| Exact costs via telemetry | `ccmetrics otel` (long-running receiver — ask before starting) |

Prefer `--json` when you will summarize; use the plain output when the user wants the report as-is.

## Rules

- Read-only by default: `ingest` runs automatically; never run `setup` or `autostart` unless the user explicitly asks to install hooks.
- Report numbers only from this turn's command output — never from memory.
- Lead with the pacing verdict (will the week / 5-hour block last), then the one or two biggest leaks with their fix templates from detector output.
- The database lives at `~/.local/share/ccmetrics/state.db` (`CCMETRICS_DB` overrides). If a command fails, show the error; do not guess at data.
