# Install notes

Python 3.11+. Zero runtime dependencies — stdlib SQLite, stdlib HTTP server, one static HTML page.

```bash
# today (from a clone — not on PyPI yet):
git clone https://github.com/flankerad/ccmetrics && cd ccmetrics
uv tool install .            # or: pipx install . / pip install -e .

# after the PyPI release:
uv tool install ccmetrics    # or: pipx install ccmetrics
```

One install covers every project on the machine. It reads all of `~/.claude/projects/`, keeps per-project data separate, and rolls it up globally in the dash.

## Working on ccmetrics itself

`uv tool install .` copies the code, so edits in your clone never reach the `ccmetrics` your status line runs. Install it linked instead:

```bash
uv tool install --force --editable .
```

Every save then shows up on the next status-line render, with no restart of Claude Code.

## The widget needs Tk

`ccmetrics widget` draws with Tk, the graphics toolkit Python normally bundles. Nothing installs it for you, and several Python builds leave it out — including the interpreter `uv` downloads for itself. Everything else in ccmetrics runs without it.

| Platform | What you need |
|---|---|
| Windows | nothing — the python.org installer includes Tk |
| macOS (Apple's own python3) | nothing |
| macOS (Homebrew) | `brew install python-tk` |
| Debian/Ubuntu | `sudo apt install python3-tk` |
| Fedora | `sudo dnf install python3-tkinter` |

If `uv tool install` picked a Python without it, point the install at one that has it:

```bash
uv tool install --force --python $(which python3) .
```

Check yours with `python3 -c "import tkinter"` — silence means it works.

## Starting at login

`ccmetrics autostart` registers the dashboard — and the widget, when your Python has Tk — to start when you log in. On macOS it writes LaunchAgents and loads them with `launchctl`. On Linux it writes systemd user units. Your first `ccmetrics` run does this once, unless you pass `--no-autostart`.

- `ccmetrics autostart --check` reads back whether the login entries are installed.
- `ccmetrics autostart --revert` removes exactly the entries ccmetrics registered, and nothing else.

## First run, and how to skip it

Type `ccmetrics` once and it sets itself up around you: it prints your summary, wires its status line into Claude Code, opens the dashboard, and opens the widget if Tk is present. It does this the first time only.

- `ccmetrics --no-dash` skips the dashboard, `CCMETRICS_NO_DASH=1` makes that permanent.
- `ccmetrics --no-setup` skips the status line, `CCMETRICS_NO_SETUP=1` makes that permanent.
- `ccmetrics --no-autostart` skips the login registration.

None of it happens when the output is piped, when you pass `--json`, or when Claude Code is not installed. In that last case ccmetrics says it found no sessions and stops.
