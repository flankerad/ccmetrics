"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, constants, ingest, report, store


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ccmetrics",
        description="Read-only leak meter for Claude Code sessions. "
        "No args: this repo's 30-day summary.",
    )
    p.add_argument("--version", action="version", version=f"ccmetrics {__version__}")
    p.add_argument(
        "--project",
        metavar="KEY",
        help="project key (encoded dir name) to report on. Keys start with '-', "
        "so pass it glued: --project=-Users-me-Dev-thing",
    )
    p.add_argument("--global", dest="global_", action="store_true", help="report across all projects")
    p.add_argument("--no-ingest", action="store_true", help="report from the store as-is")
    p.add_argument("--json", action="store_true", help="machine-readable summary")
    p.add_argument(
        "--all-leaks",
        action="store_true",
        help="list every finding, not just the top 3",
    )
    p.add_argument(
        "--evidence",
        action="store_true",
        help="with --all-leaks: print each finding's evidence JSON (counts/ids/paths only)",
    )
    sub = p.add_subparsers(dest="cmd")

    ing = sub.add_parser("ingest", help="update the store from ~/.claude/projects (read-only)")
    ing.add_argument("--stats", action="store_true", help="print ingest stats as JSON")

    d = sub.add_parser("dash", help="local dashboard on 127.0.0.1")
    d.add_argument("--port", type=int, default=7433)
    d.add_argument("--no-open", action="store_true", help="do not open a browser")
    d.add_argument("--no-ingest", action="store_true", help="serve the store as-is")

    wg = sub.add_parser("widget", help="floating always-on-top window with the hero's fuse")
    wg.add_argument("--port", type=int, default=7433, help="port the dash is serving on")
    wg.add_argument("--scope", help="project key to read, default every project")

    lv = sub.add_parser("live", help="R7 live tiles for the session running right now")
    lv.add_argument("--json", action="store_true", help="print the tile payload as JSON")

    ot = sub.add_parser(
        "otel",
        help="optional: receive Claude Code's own telemetry for exact costs "
        "(OTLP/JSON on 127.0.0.1:4318, ctrl-c to stop)",
    )
    ot.add_argument("--port", type=int, default=4318)
    ot.add_argument(
        "--setup",
        action="store_true",
        help="print the env block to enable telemetry and exit (prints only, "
        "never edits your files)",
    )
    ot.add_argument("--status", action="store_true", help="print what has arrived so far, then exit")

    sl = sub.add_parser(
        "statusline",
        help="optional: Claude Code's status line command — reads its session JSON "
        "on stdin, records your real plan %%, prints one line",
    )
    sl.add_argument(
        "--setup",
        action="store_true",
        help="print the settings.json fragment that turns this on and exit "
        "(prints only, never edits your files)",
    )
    sl.add_argument(
        "--passthrough",
        metavar="CMD",
        action="append",
        default=None,
        help="your own status line command: it is run on the same session JSON "
        "and its output is printed BEFORE ccmetrics' own line. Claude Code "
        "allows one status line command, so this keeps yours. Repeatable",
    )

    su = sub.add_parser(
        "setup",
        help="one-command installer for the status line hook (no flags: print "
        "instructions only, changes nothing)",
    )
    su.add_argument(
        "--apply",
        action="store_true",
        help="wire ccmetrics into statusLine.command, wrapping any command already there",
    )
    su.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="print the instructions and change nothing (the old default)",
    )
    su.add_argument(
        "--revert",
        action="store_true",
        help="undo --apply: restore the wrapped command, or remove statusLine if we added it",
    )
    su.add_argument(
        "--check",
        action="store_true",
        help="read-only: is the status line wired to us, and when did it last hear from Claude Code",
    )
    su.add_argument(
        "--settings",
        metavar="PATH",
        default=None,
        help="settings.json to edit (default: ~/.claude/settings.json)",
    )

    sub.add_parser("constants", help="print every constant with its source URL")
    det = sub.add_parser("detectors", help="re-run the leak detectors over the store")
    det.add_argument("--json", action="store_true", help="print findings as JSON")
    return p


def _run_ingest(conn, quiet: bool = False) -> dict:
    stats = ingest.ingest(conn)
    if not quiet:
        print(
            f"ingest: {stats['files_read']}/{stats['files_total']} files, "
            f"{stats['turns_new']:,} new turns, {stats['parse_failures']} parse failures, "
            f"{stats['elapsed_s']}s",
            file=sys.stderr,
        )
    return stats


def _render_live(t: dict) -> str:
    """One-line R7 tile row for the console (same data the dash tiles show)."""
    from . import costs

    if t.get("status") != "live":
        return f"LIVE  {t.get('status')}"
    burn = t["burn"]
    ctx = t["context"]
    rate = (
        f"{costs.fmt_tokens(burn['equiv_tokens_per_hour'])} tok/hr"
        if burn["equiv_tokens_per_hour"] is not None
        else "unknown"
    )
    if burn["floor_usd_per_hour"] is not None:
        rate += f" · at least {costs.fmt_usd(burn['floor_usd_per_hour'])}/hr"
    ctx_txt = f"{ctx['pct']:.0f}%" if ctx["pct"] is not None else "unknown"
    hit = t["cache_hit"]
    line = (
        f"LIVE  burn {rate} │ ctx {ctx_txt} │ "
        f"cache-hit {'unknown' if hit is None else f'{hit*100:.0f}%'} │ "
        f"session at least {costs.fmt_usd(t['floor_usd'])}"
    )
    lines = [line, f"      {t['turns']} turns · {t['project']} · {t['model']}"]
    w = t.get("warning")
    if w:
        lines.append(
            f"      ⚠ this turn cost more than almost every turn before it in this "
            f"session ({costs.fmt_tokens(w['equiv_tokens'])} vs the usual max "
            f"{costs.fmt_tokens(w['p90_equiv_tokens'])})"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "widget":
        # Reads the dash's own endpoint, so it never opens the store itself --
        # dispatched before the store is opened for that reason.
        from . import widget

        return widget.run(port=args.port, scope=args.scope)
    if args.cmd == "constants":
        print(json.dumps(constants.provenance(), indent=2))
        return 0
    if args.cmd == "statusline":
        # Runs on every status-line redraw, so it never opens the store unless
        # the payload actually carries plan data, and it never raises: a
        # failing statusline command is visible in the user's editor.
        from . import plan as plan_mod

        if args.setup:
            print(plan_mod.setup_text())  # print-only: no file is ever written
            return 0
        try:
            raw = sys.stdin.read()
        except Exception:
            raw = ""
        try:
            print(plan_mod.run(raw, passthrough=getattr(args, "passthrough", None)))
        except Exception:
            print("ccmetrics")
        return 0
    if args.cmd == "otel" and args.setup:
        from . import otel

        print(otel.setup_text(args.port))  # print-only: no file is ever written
        return 0
    if args.cmd == "setup":
        from pathlib import Path

        from . import plan as plan_mod

        settings_path = Path(args.settings) if args.settings else plan_mod.default_settings_path()

        if args.print_only:
            print(plan_mod.setup_text())  # print-only: no file is ever written
            return 0
        if args.apply or not (args.revert or args.check):
            try:
                result = plan_mod.apply_setup(settings_path)
            except plan_mod.SetupError as e:
                print(f"ccmetrics setup --apply: {e}")
                return 1
            print(result["message"])
            return 0
        if args.revert:
            try:
                result = plan_mod.revert_setup(settings_path)
            except plan_mod.SetupError as e:
                print(f"ccmetrics setup --revert: {e}")
                return 1
            print(result["message"])
            return 0
        if args.check:
            conn = store.connect()
            try:
                print(plan_mod.check_setup(settings_path, conn))
            finally:
                conn.close()
            return 0
        print(plan_mod.setup_text())  # print-only: no file is ever written
        return 0
    conn = store.connect()
    try:
        if args.cmd == "otel":
            from . import otel

            if args.status:
                st = store.otel_stats(conn)
                st["receiver_live"] = otel.receiver_live(args.port)
                print(json.dumps(st, indent=2, default=str))
                return 0
            conn.close()  # the receiver opens its own thread-shared writer
            return otel.serve(port=args.port)

        if args.cmd == "detectors":
            from . import detectors

            stats = detectors.run_and_store(conn)
            if args.json:
                print(json.dumps(store.load_findings(conn), indent=2, default=str))
            else:
                print(json.dumps(stats, indent=2))
            return 0

        if args.cmd == "ingest":
            stats = _run_ingest(conn, quiet=args.stats)
            if args.stats:
                print(json.dumps(stats, indent=2, default=str))
            return 0

        if args.cmd == "live":
            from . import live as live_mod

            tiles = live_mod.tiles(conn, args.project)
            if args.json:
                print(json.dumps(tiles, indent=2, default=str))
            else:
                print(_render_live(tiles))
            return 0

        if args.cmd == "dash":
            from . import dash

            if not args.no_ingest:
                _run_ingest(conn)
            conn.close()  # the server opens its own connection per thread
            if args.no_ingest:
                # serve the store as-is: no periodic re-ingest either.
                return dash.serve(port=args.port, open_browser=not args.no_open, reingest_period=None)
            return dash.serve(port=args.port, open_browser=not args.no_open)

        if not args.no_ingest:
            _run_ingest(conn)

        project = args.project
        if not project and not args.global_:
            here = report.current_project_key()
            if here in report.known_projects(conn):
                project = here
        s = report.summary(conn, project)
        projects = report.top_projects(conn) if project is None else None
        found = (
            report.all_leaks(conn, project)
            if args.all_leaks
            else report.leaks(conn, project, limit=3)
        )

        if args.json:
            print(
                json.dumps(
                    {"summary": s, "top_projects": projects, "findings": found},
                    indent=2,
                    default=str,
                )
            )
            return 0

        if args.all_leaks:
            scope = project or "all projects"
            print(f"ccmetrics · all findings · {scope}")
            print("")
            print("\n".join(report.render_leaks(found, scoped=project is not None)))
            if args.evidence:
                for f in found:
                    print(f"--- detector {f['detector']} · {f['project']} · evidence")
                    print(json.dumps(json.loads(f["evidence"]), indent=2, sort_keys=True))
            return 0

        size = store.db_path().stat().st_size if store.db_path().exists() else None
        print(
            report.render(
                s, projects, db_size=size, found=found,
                plan=store.latest_plan_windows(conn),
            )
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
