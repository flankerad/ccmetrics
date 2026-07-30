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
    sub = p.add_subparsers(dest="cmd")

    ing = sub.add_parser("ingest", help="update the store from ~/.claude/projects (read-only)")
    ing.add_argument("--stats", action="store_true", help="print ingest stats as JSON")

    d = sub.add_parser("dash", help="local dashboard (wave C)")
    d.add_argument("--port", type=int, default=7433)

    sub.add_parser("constants", help="print every constant with its source URL")
    sub.add_parser("detectors", help="leak detectors (wave B)")
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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "constants":
        print(json.dumps(constants.provenance(), indent=2))
        return 0
    if args.cmd == "detectors":
        print("detectors land in wave B (PRD R3: 12 detectors + paste-ready fixes).")
        return 0

    conn = store.connect()
    try:
        if args.cmd == "ingest":
            stats = _run_ingest(conn, quiet=args.stats)
            if args.stats:
                print(json.dumps(stats, indent=2, default=str))
            return 0

        if args.cmd == "dash":
            from . import dash

            return dash.serve(port=args.port)

        if not args.no_ingest:
            _run_ingest(conn)

        project = args.project
        if not project and not args.global_:
            here = report.current_project_key()
            if here in report.known_projects(conn):
                project = here
        s = report.summary(conn, project)
        projects = report.top_projects(conn) if project is None else None

        if args.json:
            print(json.dumps({"summary": s, "top_projects": projects}, indent=2, default=str))
            return 0

        size = store.db_path().stat().st_size if store.db_path().exists() else None
        print(report.render(s, projects, db_size=size))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
