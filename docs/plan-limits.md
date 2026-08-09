# Plan limits

How much of your Pro/Max plan is gone is **not** in any file on your machine — it lives on Anthropic's servers. So ccmetrics never estimates it. There is exactly one honest local source: Claude Code hands its status-line command its own session JSON, and for subscribers that JSON carries `rate_limits` — the same percentages `/usage` shows.

## What the status line prints

```
ccmetrics > main | ✳ Opus 5 :: default 620k/1M (62%) | $21.25 | 5h 24% left | ████▌░░░ week 26% left
```

Repo, branch, model, output style, tokens used against the context window, what this session has cost so far, then how much of your 5-hour and weekly plan limits remains, with an eight-cell bar beside the weekly figure.

- Every segment is optional. When a number is missing the segment disappears and the separators close up behind it.
- The bar fills left to right in eighths of a cell, so it moves every percent or two rather than sitting still.
- Each cell is coloured by its own position: the leftmost cells stay green, and only a nearly-spent week shows red.
- The bar tracks the WEEKLY percentage, not context. A 200k-token context window dwarfs one conversation, so a context-driven bar would barely move, while the week number climbs over days.
- No subscription means no weekly reading, so no bar prints at all. Context stays a plain number either way.
- The plan segments print what is LEFT but colour by what is SPENT, so a week with 5% left reads red, not green.
- The dollar figure stays plain because it has no ceiling to grade against.

`ccmetrics dash` grows a **PLAN** card and `ccmetrics` prints a PLAN line from the same reading. Only the percentage, the reset time and the session id are stored (90 days, metadata as always). Readings older than 6 hours are labelled stale rather than shown as current.

## Where the percentage comes from

- It arrives only when Claude Code actually renders your status line. An idle session writes nothing, so the dash and widget age until the next render.
- ccmetrics reads only what the terminal Claude Code writes on THIS machine. Work done in claude.ai on the web never reaches it.
- The percentage is account-wide and computed by Anthropic. The local files carry a copy of it and never derive or estimate it.

## It turns itself on

Claude Code runs one status-line command, so wiring ccmetrics in by hand would mean editing JSON. The first time you run `ccmetrics`, it does that for you and says so:

```
ccmetrics wired its status line into ~/.claude/settings.json — your plan usage (5h and weekly %) now shows there.
undo any time: ccmetrics setup --revert
```

It tries this exactly once, ever. It stays quiet when nobody is watching: piped output, `--json`, and the status-line hook itself never trigger it. `CCMETRICS_NO_SETUP=1` or `ccmetrics --no-setup` skips it entirely. `ccmetrics setup --check` tells you where things stand, including when a plan % was last seen. Skipped it and changed your mind? `ccmetrics setup --apply` wires it by hand.

## What the wiring does

It opens `~/.claude/settings.json` (use `--settings <path>` for a different file, such as a project-level `.claude/settings.json`), saves a backup beside it (`settings.json.bak-ccmetrics`), and sets the status line to `ccmetrics statusline`.

If you already had a status line command, ccmetrics takes the slot. Claude Code renders exactly one status line, so sharing it means two tools printing on one row with the model name and cost repeated. Your old command is not lost — the backup holds it, and `--revert` puts it back. Running it twice is safe: if it is already wired, it says so and changes nothing.

It also sets `"refreshInterval": 5` on that `statusLine` block, never overriding one you set yourself. That keeps the line refreshing while a session sits idle. Claude Code's own status-line updates are event-driven and go quiet exactly when a coordinator waits on background subagents, which is often when the plan % has just moved.

## Undo, and scoping

- `ccmetrics setup --revert` restores the command it displaced, or removes the status line entirely if you had none. Delete the backup and revert still works — it just leaves you with no status line rather than your old one.
- Want ccmetrics in one project only? Claude Code reads a project's `.claude/settings.json` ahead of `~/.claude/settings.json`, so `ccmetrics setup --apply --settings .claude/settings.json` wires that project alone.
- If `ccmetrics` is not on your PATH, the wiring writes the full path to whichever `ccmetrics` you ran it with. `--check` says loudly if that ever stops being true.
