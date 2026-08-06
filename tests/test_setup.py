"""ccmetrics setup — one-command installer for the statusline hook.

`setup --apply` wires `ccmetrics statusline` into statusLine.command in a
Claude Code settings.json, wrapping any command already there. `--revert`
undoes it. `--check` reports state read-only. All of it is exercised through
`--settings <tmp_path>` so no test ever touches the real ~/.claude."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from ccmetrics import plan
from ccmetrics.cli import main

BASE = _dt.datetime(2026, 7, 31, 12, 0, 0, tzinfo=_dt.timezone.utc)


def _iso(dt: _dt.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_REAL_RESOLVE_INVOCATION = plan.resolve_invocation  # captured before any test patches it


def _write_stale_stub(tmp_path):
    """A fake `ccmetrics` build that predates --passthrough: its statusline
    --help doesn't mention the flag, and it exits nonzero (argparse-style)
    when actually given it -- reproducing the exact failure mode this
    feature closes."""
    stub = tmp_path / "bin" / "ccmetrics"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "argv = sys.argv[1:]\n"
        "if argv[:1] == ['statusline']:\n"
        "    rest = argv[1:]\n"
        "    if '--help' in rest:\n"
        "        print('usage: ccmetrics statusline [-h] [--setup]')\n"
        "        sys.exit(0)\n"
        "    if '--passthrough' in rest:\n"
        "        print('ccmetrics: error: unrecognized arguments: --passthrough', file=sys.stderr)\n"
        "        sys.exit(2)\n"
        "    sys.exit(0)\n"
        "sys.exit(0)\n"
    )
    stub.chmod(0o755)
    return stub


@pytest.fixture(autouse=True)
def _pin_invocation(monkeypatch):
    """These tests exercise the JSON/wrapping logic, not PATH lookup -- pin
    what ccmetrics decides to write itself as."""
    monkeypatch.setattr(plan, "resolve_invocation", lambda: ("ccmetrics", None))


# --- apply --------------------------------------------------------------


def test_apply_fresh_file_with_no_settings_creates_it(tmp_path):
    settings = tmp_path / "settings.json"
    result = plan.apply_setup(settings)

    assert result["changed"] is True
    data = json.loads(settings.read_text())
    assert data["statusLine"] == {"type": "command", "command": "ccmetrics statusline"}
    assert not (tmp_path / "settings.json.bak-ccmetrics").exists()  # nothing existed to back up


def test_apply_creates_missing_parent_directory(tmp_path):
    settings = tmp_path / "nested" / "dir" / "settings.json"
    result = plan.apply_setup(settings)

    assert result["changed"] is True
    assert settings.exists()


def test_apply_wraps_an_existing_third_party_command(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-tool --flag"}}))

    result = plan.apply_setup(settings)

    data = json.loads(settings.read_text())
    assert data["statusLine"]["command"] == "ccmetrics statusline --passthrough 'my-tool --flag'"
    assert result["old"] == "my-tool --flag"
    backup = tmp_path / "settings.json.bak-ccmetrics"
    assert backup.exists()
    assert json.loads(backup.read_text())["statusLine"]["command"] == "my-tool --flag"


def test_apply_preserves_unrelated_keys(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"other": {"keep": True}}))

    plan.apply_setup(settings)

    assert json.loads(settings.read_text())["other"] == {"keep": True}


def test_apply_twice_is_idempotent_no_double_wrap(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-tool --flag"}}))

    plan.apply_setup(settings)
    result2 = plan.apply_setup(settings)

    assert result2["changed"] is False
    command = json.loads(settings.read_text())["statusLine"]["command"]
    assert command == "ccmetrics statusline --passthrough 'my-tool --flag'"
    assert command.count("--passthrough") == 1


def test_apply_command_with_single_quotes_round_trips(tmp_path):
    settings = tmp_path / "settings.json"
    original = "echo 'hi there' | some-tool"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": original}}))

    plan.apply_setup(settings)

    wrapped = json.loads(settings.read_text())["statusLine"]["command"]
    assert plan._extract_passthrough(wrapped) == original


def test_apply_unparseable_json_refused_and_file_untouched(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")

    with pytest.raises(plan.SetupError):
        plan.apply_setup(settings)

    assert settings.read_text() == "{not json"
    assert not (tmp_path / "settings.json.bak-ccmetrics").exists()


def test_apply_unknown_statusline_shape_refused(tmp_path):
    settings = tmp_path / "settings.json"
    original = json.dumps({"statusLine": "just-a-string"})
    settings.write_text(original)

    with pytest.raises(plan.SetupError):
        plan.apply_setup(settings)

    assert settings.read_text() == original


def test_apply_statusline_wrong_type_refused(tmp_path):
    settings = tmp_path / "settings.json"
    original = json.dumps({"statusLine": {"type": "script", "command": "x"}})
    settings.write_text(original)

    with pytest.raises(plan.SetupError):
        plan.apply_setup(settings)

    assert settings.read_text() == original


# --- revert ---------------------------------------------------------------


def test_revert_restores_the_original_wrapped_command(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-tool --flag"}}))
    plan.apply_setup(settings)

    result = plan.revert_setup(settings)

    assert result["changed"] is True
    assert json.loads(settings.read_text())["statusLine"]["command"] == "my-tool --flag"


def test_revert_removes_statusline_when_we_added_it_outright(tmp_path):
    settings = tmp_path / "settings.json"
    plan.apply_setup(settings)  # fresh file, no prior statusLine

    result = plan.revert_setup(settings)

    assert result["changed"] is True
    assert "statusLine" not in json.loads(settings.read_text())


def test_revert_works_without_the_backup_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "my-tool"}}))
    plan.apply_setup(settings)
    (tmp_path / "settings.json.bak-ccmetrics").unlink()

    result = plan.revert_setup(settings)

    assert result["changed"] is True
    assert json.loads(settings.read_text())["statusLine"]["command"] == "my-tool"


def test_revert_when_not_wired_is_a_noop(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": "other-tool"}}))

    result = plan.revert_setup(settings)

    assert result["changed"] is False
    assert json.loads(settings.read_text())["statusLine"]["command"] == "other-tool"


def test_revert_missing_file_is_a_noop(tmp_path):
    result = plan.revert_setup(tmp_path / "settings.json")
    assert result["changed"] is False


# --- check ------------------------------------------------------------------


def test_check_reports_not_wired(tmp_path):
    text = plan.check_setup(tmp_path / "settings.json")
    assert "not wired" in text.lower()


def test_check_reports_wired(tmp_path):
    settings = tmp_path / "settings.json"
    plan.apply_setup(settings)

    text = plan.check_setup(settings)

    assert "wired to ccmetrics" in text


def test_check_reports_plan_reading_freshness(conn, tmp_path):
    settings = tmp_path / "settings.json"
    data = plan.extract({"rate_limits": {"five_hour": {"used_percentage": 20, "resets_at": None}}})
    plan.record(conn, data, now_iso=_iso(BASE))

    text = plan.check_setup(settings, conn=conn)

    assert "most recent plan reading stored" in text
    assert _iso(BASE) in text


def test_check_no_plan_reading_yet(conn, tmp_path):
    text = plan.check_setup(tmp_path / "settings.json", conn=conn)
    assert "no plan reading stored yet" in text


def test_check_warns_when_command_does_not_resolve(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "/no/such/path/ccmetrics statusline"}})
    )

    text = plan.check_setup(settings)

    assert "WARNING" in text
    assert "does not resolve" in text


def test_check_warns_when_resolved_build_predates_passthrough(tmp_path):
    """The command resolves (the file exists and runs) but is a stale build
    that doesn't understand --passthrough -- --check must not stop at "it
    resolves"."""
    stub = _write_stale_stub(tmp_path)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {"statusLine": {"type": "command", "command": f"{stub} statusline --passthrough 'my-tool'"}}
        )
    )

    text = plan.check_setup(settings)

    assert "WARNING" in text
    assert "does not support --passthrough" in text


def test_check_probes_the_resolved_path_when_command_uses_a_bare_name(tmp_path, monkeypatch):
    """When settings.json wires a bare `ccmetrics statusline` command,
    --check must probe the file that name resolves to on PATH, not the
    bare name itself (which would silently re-resolve through the real
    PATH, ignoring what `shutil.which` actually reported)."""
    stub = _write_stale_stub(tmp_path)
    monkeypatch.setattr(plan.shutil, "which", lambda name: str(stub) if name == "ccmetrics" else None)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "ccmetrics statusline --passthrough 'my-tool'"}})
    )

    text = plan.check_setup(settings)

    assert "WARNING" in text
    assert "does not support --passthrough" in text


def test_check_says_nothing_extra_when_resolved_build_is_current(tmp_path):
    # the venv's own console script genuinely supports --passthrough
    venv_ccmetrics = Path(plan.sys.executable).parent / "ccmetrics"
    if not venv_ccmetrics.exists():
        pytest.skip("no console-script build alongside this interpreter")
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "statusLine": {
                    "type": "command",
                    "command": f"{venv_ccmetrics} statusline --passthrough 'my-tool'",
                }
            }
        )
    )

    text = plan.check_setup(settings)

    assert "does not support --passthrough" not in text


# --- resolve_invocation -----------------------------------------------------


def test_resolve_invocation_prefers_bare_command_when_the_build_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(plan, "resolve_invocation", _REAL_RESOLVE_INVOCATION)
    exe = tmp_path / "ccmetrics"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plan.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(plan.sys, "argv", [str(exe)])
    assert plan.resolve_invocation() == ("ccmetrics", None)


def test_resolve_invocation_falls_back_to_argv0_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(plan, "resolve_invocation", _REAL_RESOLVE_INVOCATION)
    fake = tmp_path / "ccmetrics"
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plan.shutil, "which", lambda name: None)
    monkeypatch.setattr(plan.sys, "argv", [str(fake)])
    assert plan.resolve_invocation() == (str(fake.resolve()), None)


def test_resolve_invocation_falls_back_when_path_build_is_stale(monkeypatch, tmp_path):
    """The build on PATH resolves but rejects --passthrough (an older
    install shadowing this checkout) -- resolve_invocation must not hand
    back the bare, unsafe `ccmetrics` in that case."""
    monkeypatch.setattr(plan, "resolve_invocation", _REAL_RESOLVE_INVOCATION)
    running = tmp_path / "running-ccmetrics"
    running.write_text("#!/bin/sh\n")
    stale = tmp_path / "stale-ccmetrics"
    stale.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plan.shutil, "which", lambda name: str(stale))
    monkeypatch.setattr(plan.sys, "argv", [str(running)])
    monkeypatch.setattr(plan, "_probe_supports_passthrough", lambda exe: False)

    invocation, warning = plan.resolve_invocation()

    assert invocation == str(running.resolve())
    assert warning is not None
    assert "does not support --passthrough" in warning
    assert str(stale) in warning


def test_resolve_invocation_warns_when_probe_is_inconclusive(monkeypatch, tmp_path):
    monkeypatch.setattr(plan, "resolve_invocation", _REAL_RESOLVE_INVOCATION)
    running = tmp_path / "running-ccmetrics"
    running.write_text("#!/bin/sh\n")
    other = tmp_path / "other-ccmetrics"
    other.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plan.shutil, "which", lambda name: str(other))
    monkeypatch.setattr(plan.sys, "argv", [str(running)])
    monkeypatch.setattr(plan, "_probe_supports_passthrough", lambda exe: None)

    invocation, warning = plan.resolve_invocation()

    assert invocation == str(running.resolve())
    assert warning is not None


def test_resolve_invocation_probes_the_resolved_path_not_the_bare_name(monkeypatch, tmp_path):
    """The probe must test the exact file `shutil.which` resolved to, not
    re-resolve the bare name `ccmetrics` through PATH itself -- otherwise a
    stubbed/monkeypatched `which` result is silently ignored and a
    different binary gets probed instead."""
    monkeypatch.setattr(plan, "resolve_invocation", _REAL_RESOLVE_INVOCATION)
    running = tmp_path / "running-ccmetrics"
    running.write_text("#!/bin/sh\n")
    stale = tmp_path / "stale-ccmetrics"
    stale.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plan.shutil, "which", lambda name: str(stale))
    monkeypatch.setattr(plan.sys, "argv", [str(running)])

    seen_exe = []

    def fake_probe(exe_tokens):
        seen_exe.append(exe_tokens)
        return False

    monkeypatch.setattr(plan, "_probe_supports_passthrough", fake_probe)

    plan.resolve_invocation()

    assert seen_exe == [[str(stale)]]


def test_apply_writes_absolute_path_when_path_build_is_stale(monkeypatch, tmp_path):
    """--apply must not wire up a bare `ccmetrics` that PATH resolves to a
    build too old to support --passthrough -- it should prefer the absolute
    path of the checkout actually running --apply, and say why."""
    stub = _write_stale_stub(tmp_path)
    monkeypatch.setattr(plan, "resolve_invocation", _REAL_RESOLVE_INVOCATION)
    monkeypatch.setattr(plan.shutil, "which", lambda name: str(stub))
    running = tmp_path / "checkout" / "running-ccmetrics"
    running.parent.mkdir()
    running.write_text("#!/bin/sh\n")
    monkeypatch.setattr(plan.sys, "argv", [str(running)])

    result = plan.apply_setup(tmp_path / "settings.json")

    assert result["invocation_warning"] is not None
    assert "does not support --passthrough" in result["invocation_warning"]
    assert result["new"] == f"{running.resolve()} statusline"
    assert result["invocation_warning"] in result["message"]


# --- cli wiring ---------------------------------------------------------


def test_cli_setup_print_prints_instructions_and_changes_nothing(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    assert main(["setup", "--print", "--settings", str(settings)]) == 0
    assert not settings.exists()
    assert "ccmetrics statusline" in capsys.readouterr().out


def test_cli_setup_no_flags_wires_the_status_line(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    assert main(["setup", "--settings", str(settings)]) == 0
    capsys.readouterr()
    assert "ccmetrics statusline" in settings.read_text()


def test_cli_setup_apply_then_check_round_trip(cc_env, tmp_path, capsys):
    settings = tmp_path / "settings.json"
    assert main(["setup", "--apply", "--settings", str(settings)]) == 0
    capsys.readouterr()

    assert main(["setup", "--check", "--settings", str(settings)]) == 0
    out = capsys.readouterr().out
    assert "wired to ccmetrics" in out


def test_cli_setup_apply_refuses_bad_json_and_exits_nonzero(tmp_path, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text("{not json")

    rc = main(["setup", "--apply", "--settings", str(settings)])

    assert rc != 0
    assert settings.read_text() == "{not json"
