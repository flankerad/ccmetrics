---
name: ccmetrics
description: Answer questions about Claude Code plan pacing, token usage, costs, and leaks by running the local ccmetrics CLI. Use when the user asks "will my plan last", "how much have I used", "what's burning tokens", "any leaks", or wants a usage summary, live session status, or the dashboard/widget opened.
---

# ccmetrics skill

ccmetrics is this repo's tool: a leak meter for Claude Code sessions. Reporting only reads; the `setup` and `autostart` commands install things. It is already installed as the `ccmetrics` command. Run it; never re-implement its analysis.

## First, make it self-maintaining

Plan percentages only stay fresh if the status line and the login services are wired. On your first ccmetrics question in a session, check both and fix what is missing — read the text they print, not the exit code (both return 0 either way):

| Check | If it does not say it is wired |
|---|---|
| `ccmetrics setup --check` | see the status line rule below |
| `ccmetrics autostart --check` | run `ccmetrics autostart --apply` — no conflict to worry about, it only adds two login services |

Status line rule, because Claude Code renders exactly one status line command:
- slot empty, or already `ccmetrics statusline` → run `ccmetrics setup --apply`, say one line about it.
- slot held by another tool → do NOT apply silently. Name that command, say it will be saved to `~/.claude/settings.json.bak-ccmetrics` and that `ccmetrics setup --revert` puts it back, and ask before applying.
- user says no → skip it and answer from `ccmetrics` alone; the plan % will just be as fresh as the last reading.

Do this once per session, not per question.

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

- `ingest` runs automatically. `setup` and `autostart` are how plan % keeps refreshing — wire them per the section above. `setup` replaces the current status line command (the old one is kept at `~/.claude/settings.json.bak-ccmetrics`; `ccmetrics setup --revert` restores it, or clears the slot when no usable backup exists); `autostart` installs two login services — one keeps the dash alive, one opens the widget. `ccmetrics autostart --revert` removes exactly the services it registered.
- Report numbers only from this turn's command output — never from memory.
- Lead with the pacing verdict (will the week / 5-hour block last), then the one or two biggest leaks with their fix templates from detector output.
- The database lives at `~/.local/share/ccmetrics/state.db` (`CCMETRICS_DB` overrides). If a command fails, show the error; do not guess at data.
