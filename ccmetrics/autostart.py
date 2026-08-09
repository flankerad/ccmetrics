"""Autostart at login — `ccmetrics autostart`.

`cli._first_run_dash` serves the dash and opens the widget on first run, but
it does that in the FOREGROUND: closing the widget ends the dash with it,
and none of it survives a reboot. This module closes that gap by registering
two per-user login services -- one that keeps the dash running in the
background, one that opens the widget -- so both come back on their own
after a login, the way the status line wires itself in `plan.py`
(`apply_setup`/`revert_setup`/`setup_text`/`_backup`). Same shape here:
`apply()`/`revert()`/`check()`, the same "writes only its own files, backs
up nothing it did not create" honesty, and every write idempotent -- calling
`apply()` twice changes nothing and says so.

The dash service runs `ccmetrics dash --no-open` so a login never throws a
browser tab at the user, and it restarts if it dies (macOS `KeepAlive`,
Linux `Restart=on-failure`). The widget is a separate entry because it needs
a GUI session and Tk -- and it is NOT restart-on-exit: the user closes it
deliberately with the X, and a restart loop would fight them. When Tk is
missing (same detection `widget.run` itself uses), only the dash service is
registered, and callers are told so.

macOS gets two LaunchAgent plists under ~/Library/LaunchAgents/, picked up
by launchd automatically at the user's next login -- nothing here ever
calls `launchctl`, the same non-invasive shape as `plan.py`, which never
reloads Claude Code either. Linux gets two systemd user units under
~/.config/systemd/user/, made effective by hand-linking them into
`default.target.wants/` (exactly what `systemctl --user enable` would
create) rather than shelling out to `systemctl`. Any other platform
(Windows included) writes nothing and says it isn't supported yet -- never
a half-valid file.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

DASH_LABEL = "com.ccmetrics.dash"
WIDGET_LABEL = "com.ccmetrics.widget"
DASH_UNIT = "ccmetrics-dash.service"
WIDGET_UNIT = "ccmetrics-widget.service"


# --- platform + executable resolution ---------------------------------------


def _platform_kind() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unsupported"


def _exe_argv() -> list[str]:
    """Absolute-path argv prefix for invoking this ccmetrics build.

    Mirrors the fallback half of `plan.resolve_invocation`: login services
    don't inherit the interactive shell's PATH, so a bare `ccmetrics` here
    would silently never run. Always resolves to an absolute path (or, if
    this process wasn't started from a real, directly-executable file,
    `<python> -m ccmetrics`), never the PATH-relative shortcut the status
    line prefers.

    Unlike `resolve_invocation`, this feeds an argv ARRAY straight to
    launchd/systemd, never a shell -- so `sys.argv[0]` must itself be
    something the OS can exec directly. Under `python -m ccmetrics`,
    `sys.argv[0]` is the package's `__main__.py`: it exists on disk but has
    no +x bit and no shebang, so execing it directly would fail silently at
    login. `os.access(..., X_OK)` catches that case the same way a plain
    `.exists()` check would not.
    """
    self_exe = Path(sys.argv[0]).resolve()
    if self_exe.exists() and os.access(self_exe, os.X_OK):
        return [str(self_exe)]
    return [sys.executable, "-m", "ccmetrics"]


def _has_tk() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    return True


# --- paths --------------------------------------------------------------


def _launch_agents_dir(home: Path) -> Path:
    return home / "Library" / "LaunchAgents"


def _systemd_unit_dir(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _systemd_wants_dir(home: Path) -> Path:
    return _systemd_unit_dir(home) / "default.target.wants"


def _log_dir(home: Path) -> Path:
    return home / "Library" / "Logs" / "ccmetrics"


# --- idempotent writers ---------------------------------------------------


def _write_if_changed(path: Path, content: bytes) -> bool:
    """Writes `content` to `path`, returns whether anything changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return False
    path.write_bytes(content)
    return True


def _link_if_changed(link: Path, target: Path) -> bool:
    """Symlinks `link` -> `target`, returns whether anything changed."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.readlink() == target:
        return False
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)
    return True


# --- macOS: LaunchAgent plists -----------------------------------------------


def _plist_bytes(label: str, argv: list[str], keep_alive: bool, gui_only: bool, log_path: Path) -> bytes:
    data = {
        "Label": label,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
        # Without these, a crash at login (the Tcl/Tk path bug this same
        # session hit) leaves launchd's own record empty -- the process just
        # vanishes and there is nothing to grep. Both streams go to the same
        # per-label file; stdout and stderr interleaved is still more than
        # the nothing there was before.
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    if gui_only:
        # The widget needs a real GUI session (Tk draws a window); running it
        # from a headless/background launchd context would just fail.
        data["LimitLoadToSessionType"] = "Aqua"
    return plistlib.dumps(data)


def _bootstrap_macos(plist_path: Path) -> str | None:
    """Loads a just-written plist immediately via launchd, so the user does
    not have to log out and back in to see it take effect. Returns an error
    string on failure, None on success -- callers treat failure as
    non-fatal and report it rather than raising, since the file is still
    correctly on disk for the NEXT login either way."""
    uid = os.getuid()
    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True, text=True,
        )
    except OSError as e:  # no launchctl on PATH -- shouldn't happen on a real Mac
        return str(e)
    if result.returncode != 0:
        # Already-loaded is the overwhelmingly common "failure": re-applying
        # an unchanged plist should not be treated as an error. launchd
        # reports it in stderr, not a distinct exit code, so string-match it.
        stderr = (result.stderr or "").strip()
        if "already bootstrapped" in stderr.lower():
            return None
        return stderr or f"launchctl exited {result.returncode}"
    return None


def _apply_macos(home: Path, argv: list[str], has_tk: bool) -> tuple[list[str], list[str], list[str]]:
    """Returns (written, present, bootstrap_errors) -- absolute paths that
    changed, every path this module manages that exists after the call, and
    any non-fatal errors loading a written plist immediately."""
    agents = _launch_agents_dir(home)
    log_dir = _log_dir(home)
    log_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    present: list[str] = []
    bootstrap_errors: list[str] = []

    dash_path = agents / f"{DASH_LABEL}.plist"
    dash_bytes = _plist_bytes(
        DASH_LABEL, [*argv, "dash", "--no-open"], keep_alive=True, gui_only=False,
        log_path=log_dir / f"{DASH_LABEL}.log",
    )
    if _write_if_changed(dash_path, dash_bytes):
        written.append(str(dash_path))
        err = _bootstrap_macos(dash_path)
        if err:
            bootstrap_errors.append(f"{dash_path}: {err}")
    present.append(str(dash_path))

    if has_tk:
        widget_path = agents / f"{WIDGET_LABEL}.plist"
        widget_bytes = _plist_bytes(
            WIDGET_LABEL, [*argv, "widget"], keep_alive=False, gui_only=True,
            log_path=log_dir / f"{WIDGET_LABEL}.log",
        )
        if _write_if_changed(widget_path, widget_bytes):
            written.append(str(widget_path))
            err = _bootstrap_macos(widget_path)
            if err:
                bootstrap_errors.append(f"{widget_path}: {err}")
        present.append(str(widget_path))

    return written, present, bootstrap_errors


def _revert_macos(home: Path) -> list[str]:
    agents = _launch_agents_dir(home)
    removed = []
    for label in (DASH_LABEL, WIDGET_LABEL):
        p = agents / f"{label}.plist"
        if p.exists():
            p.unlink()
            removed.append(str(p))
    return removed


def _check_macos(home: Path) -> tuple[bool, bool]:
    agents = _launch_agents_dir(home)
    return (
        (agents / f"{DASH_LABEL}.plist").exists(),
        (agents / f"{WIDGET_LABEL}.plist").exists(),
    )


# --- Linux: systemd user units ----------------------------------------------


def _dash_unit_text(argv: list[str]) -> str:
    cmd = " ".join(argv + ["dash", "--no-open"])
    return (
        "[Unit]\n"
        "Description=ccmetrics dashboard (autostart)\n"
        "\n"
        "[Service]\n"
        f"ExecStart={cmd}\n"
        "Restart=on-failure\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _widget_unit_text(argv: list[str]) -> str:
    cmd = " ".join(argv + ["widget"])
    return (
        "[Unit]\n"
        "Description=ccmetrics widget (autostart)\n"
        "\n"
        "[Service]\n"
        f"ExecStart={cmd}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _start_linux(name: str) -> str | None:
    """`systemctl --user enable --now` on the freshly-written unit, so it is
    running THIS session rather than only at the next login. Mirrors
    `_bootstrap_macos`: non-fatal, returns an error string on failure rather
    than raising, since the unit file is still correctly on disk either way
    and will take effect at the next login regardless."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "enable", "--now", name],
            capture_output=True, text=True,
        )
    except OSError as e:  # no systemd on this box (or this test's platform)
        return str(e)
    if result.returncode != 0:
        return (result.stderr or "").strip() or f"systemctl exited {result.returncode}"
    return None


def _apply_linux(home: Path, argv: list[str], has_tk: bool) -> tuple[list[str], list[str], list[str]]:
    unit_dir = _systemd_unit_dir(home)
    wants_dir = _systemd_wants_dir(home)
    written: list[str] = []
    present: list[str] = []
    bootstrap_errors: list[str] = []

    units = [(DASH_UNIT, _dash_unit_text(argv))]
    if has_tk:
        units.append((WIDGET_UNIT, _widget_unit_text(argv)))

    for name, text in units:
        unit_path = unit_dir / name
        unit_written = _write_if_changed(unit_path, text.encode())
        if unit_written:
            written.append(str(unit_path))
        present.append(str(unit_path))

        link = wants_dir / name
        # `systemctl --user enable` would create exactly this symlink; doing
        # it by hand means WantedBy=default.target takes effect without ever
        # shelling out to systemctl for the enable half -- only --now (the
        # part with no idempotent file-write equivalent) shells out.
        if _link_if_changed(link, Path("..") / name):
            written.append(str(link))
        present.append(str(link))

        if unit_written:
            err = _start_linux(name)
            if err:
                bootstrap_errors.append(f"{unit_path}: {err}")

    return written, present, bootstrap_errors


def _revert_linux(home: Path) -> list[str]:
    unit_dir = _systemd_unit_dir(home)
    wants_dir = _systemd_wants_dir(home)
    removed = []
    for name in (DASH_UNIT, WIDGET_UNIT):
        link = wants_dir / name
        if link.is_symlink() or link.exists():
            link.unlink()
            removed.append(str(link))
        unit_path = unit_dir / name
        if unit_path.exists():
            unit_path.unlink()
            removed.append(str(unit_path))
    return removed


def _check_linux(home: Path) -> tuple[bool, bool]:
    unit_dir = _systemd_unit_dir(home)
    return (
        (unit_dir / DASH_UNIT).exists(),
        (unit_dir / WIDGET_UNIT).exists(),
    )


# --- apply / revert / check --------------------------------------------------


def apply(home: Path | None = None) -> dict:
    """Registers the login services. Returns {"changed": bool, "message": str, ...}.

    Idempotent: calling this twice writes nothing the second time and says
    so. Never raises -- an unsupported platform is a normal (changed=False)
    result, not an error.
    """
    home = Path(home) if home else Path.home()
    kind = _platform_kind()
    if kind == "unsupported":
        return {
            "changed": False,
            "platform": kind,
            "message": f"autostart is not supported on this platform yet ({sys.platform}).",
        }

    argv = _exe_argv()
    has_tk = _has_tk()
    if kind == "macos":
        written, present, bootstrap_errors = _apply_macos(home, argv, has_tk)
    else:
        written, present, bootstrap_errors = _apply_linux(home, argv, has_tk)

    lines = []
    if written:
        lines += [f"wrote {p}" for p in written]
        if not has_tk:
            lines.append(
                "tkinter is missing on this Python build -- registered the "
                "dashboard service only, not the widget."
            )
        # Bootstrap/enable failures are reported but don't flip `changed` to
        # False -- the files are correctly on disk and WILL take effect at
        # the next login even if loading them right now failed.
        lines += [f"could not start now: {e}" for e in bootstrap_errors]
    else:
        lines.append("autostart is already installed -- nothing changed.")
    return {
        "changed": bool(written),
        "platform": kind,
        "has_tk": has_tk,
        "written": written,
        "present": present,
        "bootstrap_errors": bootstrap_errors,
        "message": "\n".join(lines),
    }


def revert(home: Path | None = None) -> dict:
    """Removes exactly the files `apply()` wrote. Safe to call when nothing
    is installed."""
    home = Path(home) if home else Path.home()
    kind = _platform_kind()
    if kind == "macos":
        removed = _revert_macos(home)
    elif kind == "linux":
        removed = _revert_linux(home)
    else:
        removed = []

    if not removed:
        return {
            "changed": False,
            "platform": kind,
            "message": "autostart is not installed -- nothing to revert.",
        }
    lines = [f"removed {p}" for p in removed]
    lines.append("ccmetrics no longer starts automatically at login.")
    return {"changed": True, "platform": kind, "removed": removed, "message": "\n".join(lines)}


def check(home: Path | None = None) -> str:
    """Read-only: is autostart installed. Never writes anything."""
    home = Path(home) if home else Path.home()
    kind = _platform_kind()
    if kind == "unsupported":
        return f"autostart: not supported on this platform yet ({sys.platform})."

    has_tk = _has_tk()
    if kind == "macos":
        dash_on, widget_on = _check_macos(home)
    else:
        dash_on, widget_on = _check_linux(home)

    lines = [f"dashboard autostart: {'installed' if dash_on else 'not installed'}."]
    if has_tk:
        lines.append(f"widget autostart: {'installed' if widget_on else 'not installed'}.")
    else:
        lines.append("widget autostart: skipped -- tkinter is missing on this Python build.")
    return "\n".join(lines)
