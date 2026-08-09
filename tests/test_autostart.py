"""Login autostart -- `ccmetrics autostart`.

Mirrors tests/test_setup.py in shape: apply()/revert()/check() are driven
against a tmp_path HOME, never the real ~/Library/LaunchAgents or
~/.config/systemd -- no test here may write to the developer's own machine.
"""

from __future__ import annotations

import sys

import pytest

from ccmetrics import autostart

_REAL_EXE_ARGV = autostart._exe_argv  # captured before any test patches it


@pytest.fixture(autouse=True)
def _pin_exe(monkeypatch):
    # Deterministic across machines/CI: the module under test only cares
    # that the same argv prefix lands in every file it writes.
    monkeypatch.setattr(autostart, "_exe_argv", lambda: ["/opt/ccmetrics/bin/ccmetrics"])


def _macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")


def _linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


def _tk_present(monkeypatch):
    monkeypatch.setattr(autostart, "_has_tk", lambda: True)


def _tk_missing(monkeypatch):
    monkeypatch.setattr(autostart, "_has_tk", lambda: False)


# --- macOS --------------------------------------------------------------


def test_macos_apply_writes_both_plists(tmp_path, monkeypatch):
    _macos(monkeypatch)
    _tk_present(monkeypatch)
    result = autostart.apply(tmp_path)
    assert result["changed"] is True
    agents = tmp_path / "Library" / "LaunchAgents"
    dash = agents / "com.ccmetrics.dash.plist"
    widget = agents / "com.ccmetrics.widget.plist"
    assert dash.exists()
    assert widget.exists()
    assert str(dash) in result["written"]
    assert str(widget) in result["written"]


def test_macos_plists_carry_log_paths_under_library_logs(tmp_path, monkeypatch):
    """Item 3, session 2026-08-09: a crash at login (the Tcl/Tk bug this same
    session hit) used to leave nothing behind -- no StandardOutPath/
    StandardErrorPath meant the process just vanished. Both plists now point
    at ~/Library/Logs/ccmetrics/<label>.log, and apply() creates that
    directory itself rather than assuming launchd will."""
    import plistlib

    _macos(monkeypatch)
    _tk_present(monkeypatch)
    autostart.apply(tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    log_dir = tmp_path / "Library" / "Logs" / "ccmetrics"
    assert log_dir.is_dir()
    for label, plist_name in (
        (autostart.DASH_LABEL, "com.ccmetrics.dash.plist"),
        (autostart.WIDGET_LABEL, "com.ccmetrics.widget.plist"),
    ):
        data = plistlib.loads((agents / plist_name).read_bytes())
        expected = str(log_dir / f"{label}.log")
        assert data["StandardOutPath"] == expected
        assert data["StandardErrorPath"] == expected


def test_macos_apply_twice_is_a_no_op(tmp_path, monkeypatch):
    _macos(monkeypatch)
    _tk_present(monkeypatch)
    first = autostart.apply(tmp_path)
    assert first["changed"] is True
    second = autostart.apply(tmp_path)
    assert second["changed"] is False
    assert second["written"] == []
    assert "already installed" in second["message"]


def test_macos_revert_removes_exactly_what_apply_wrote(tmp_path, monkeypatch):
    _macos(monkeypatch)
    _tk_present(monkeypatch)
    applied = autostart.apply(tmp_path)
    result = autostart.revert(tmp_path)
    assert result["changed"] is True
    assert sorted(result["removed"]) == sorted(applied["written"])
    agents = tmp_path / "Library" / "LaunchAgents"
    assert list(agents.glob("*")) == []


def test_macos_tk_missing_registers_dash_alone(tmp_path, monkeypatch):
    _macos(monkeypatch)
    _tk_missing(monkeypatch)
    result = autostart.apply(tmp_path)
    agents = tmp_path / "Library" / "LaunchAgents"
    assert (agents / "com.ccmetrics.dash.plist").exists()
    assert not (agents / "com.ccmetrics.widget.plist").exists()
    assert result["has_tk"] is False
    assert "widget" in result["message"]

    check_out = autostart.check(tmp_path)
    assert "dashboard autostart: installed." in check_out
    assert "widget autostart: skipped" in check_out


def test_macos_apply_bootstraps_each_written_plist_immediately(tmp_path, monkeypatch):
    """Item 3: the user should not have to log out to see a freshly-applied
    agent running -- apply() loads each newly-written plist via `launchctl
    bootstrap gui/<uid> <plist>` itself."""
    import subprocess

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _macos(monkeypatch)
    _tk_present(monkeypatch)
    result = autostart.apply(tmp_path)
    assert result["bootstrap_errors"] == []
    bootstrapped = {c[3] for c in calls if c[:2] == ["launchctl", "bootstrap"]}
    assert bootstrapped == set(result["written"])


def test_macos_bootstrap_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    """A bootstrap failure (e.g. no GUI session yet) must not stop apply()
    from reporting success -- the plist is still correctly on disk and will
    load at the next real login regardless."""
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such thing\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _macos(monkeypatch)
    _tk_present(monkeypatch)
    result = autostart.apply(tmp_path)
    assert result["changed"] is True
    assert len(result["bootstrap_errors"]) == 2
    assert "could not start now" in result["message"]


def test_macos_bootstrap_already_loaded_is_not_an_error(tmp_path, monkeypatch):
    import subprocess

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="already bootstrapped\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _macos(monkeypatch)
    _tk_present(monkeypatch)
    result = autostart.apply(tmp_path)
    assert result["bootstrap_errors"] == []


# --- Linux ----------------------------------------------------------------


def test_linux_apply_writes_both_units_and_enables_them(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tk_present(monkeypatch)
    result = autostart.apply(tmp_path)
    assert result["changed"] is True
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    wants_dir = unit_dir / "default.target.wants"
    assert (unit_dir / "ccmetrics-dash.service").exists()
    assert (unit_dir / "ccmetrics-widget.service").exists()
    assert (wants_dir / "ccmetrics-dash.service").is_symlink()
    assert (wants_dir / "ccmetrics-widget.service").is_symlink()


def test_linux_apply_twice_is_a_no_op(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tk_present(monkeypatch)
    autostart.apply(tmp_path)
    second = autostart.apply(tmp_path)
    assert second["changed"] is False
    assert second["written"] == []


def test_linux_revert_removes_exactly_what_apply_wrote(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tk_present(monkeypatch)
    applied = autostart.apply(tmp_path)
    result = autostart.revert(tmp_path)
    assert result["changed"] is True
    assert sorted(result["removed"]) == sorted(applied["written"])
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert not (unit_dir / "ccmetrics-dash.service").exists()
    assert not (unit_dir / "ccmetrics-widget.service").exists()
    assert not (unit_dir / "default.target.wants" / "ccmetrics-dash.service").exists()


def test_linux_tk_missing_registers_dash_alone(tmp_path, monkeypatch):
    _linux(monkeypatch)
    _tk_missing(monkeypatch)
    result = autostart.apply(tmp_path)
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / "ccmetrics-dash.service").exists()
    assert not (unit_dir / "ccmetrics-widget.service").exists()
    assert result["has_tk"] is False


def test_linux_apply_enables_now_each_written_unit(tmp_path, monkeypatch):
    """Item 3's Linux mirror: `systemctl --user enable --now <unit>` on each
    newly-written unit, so it is running THIS session, not only the next
    login."""
    import subprocess

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _linux(monkeypatch)
    _tk_present(monkeypatch)
    result = autostart.apply(tmp_path)
    assert result["bootstrap_errors"] == []
    started = {c[4] for c in calls if c[:4] == ["systemctl", "--user", "enable", "--now"]}
    assert started == {"ccmetrics-dash.service", "ccmetrics-widget.service"}


# --- _exe_argv ------------------------------------------------------------


def test_exe_argv_uses_the_absolute_path_when_it_is_directly_executable(tmp_path, monkeypatch):
    exe = tmp_path / "ccmetrics"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "argv", [str(exe)])
    assert _REAL_EXE_ARGV() == [str(exe.resolve())]


def test_exe_argv_falls_back_when_sys_argv_0_is_not_executable(tmp_path, monkeypatch):
    """`python -m ccmetrics` sets sys.argv[0] to the package's own
    __main__.py -- a real file on disk, but with no +x bit, so launchd or
    systemd could never exec it directly the way ProgramArguments/ExecStart
    require."""
    not_exe = tmp_path / "__main__.py"
    not_exe.write_text("# not executable\n")
    monkeypatch.setattr(sys, "argv", [str(not_exe)])
    assert _REAL_EXE_ARGV() == [sys.executable, "-m", "ccmetrics"]


# --- shared: nothing installed / unsupported --------------------------------


def test_revert_with_nothing_installed_is_clean(tmp_path, monkeypatch):
    _macos(monkeypatch)
    result = autostart.revert(tmp_path)
    assert result["changed"] is False
    assert "nothing to revert" in result["message"]


def test_unsupported_platform_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    result = autostart.apply(tmp_path)
    assert result["changed"] is False
    assert "not supported" in result["message"]
    assert list(tmp_path.glob("**/*")) == []

    assert "not supported" in autostart.check(tmp_path)

    revert_result = autostart.revert(tmp_path)
    assert revert_result["changed"] is False
