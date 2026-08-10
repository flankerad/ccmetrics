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
| What's leaking / wasting tokens | `ccmetrics` (top leaks, ranked) — `ccmetrics --all-leaks` for every leak, `--all-leaks --evidence` to add proof lines. These are top-level flags: `ccmetrics detectors --all-leaks` fails |
| Current session, live | `ccmetrics live` |
| Open the dashboard | `ccmetrics dash` (127.0.0.1:7433, run in background) |
| Open the floating widget | `ccmetrics widget` (run in background) |
| Exact costs via telemetry | `ccmetrics otel` (long-running receiver — ask before starting) |

Default to the plain output — it carries the PLAN line and the leak fix templates. `--json` returns only `summary`, `top_projects`, `findings`: no plan percentages, no fix text, so never answer a pacing question from it.

`ccmetrics detectors` is a diagnostic dump (finding counts per detector id), not the leak report — do not use it to answer "what's leaking".

## Rules

- `ingest` runs automatically. `setup` and `autostart` are how plan % keeps refreshing — run them when the user wants that. `setup` replaces the current status line command (the old one is kept at `~/.claude/settings.json.bak-ccmetrics`, `ccmetrics setup --revert` restores it); `autostart` installs a login agent.
- Report numbers only from this turn's command output — never from memory.
- Lead with the pacing verdict (will the week / 5-hour block last), then the one or two biggest leaks with their fix templates from detector output.
- The database lives at `~/.local/share/ccmetrics/state.db` (`CCMETRICS_DB` overrides). If a command fails, show the error; do not guess at data.
