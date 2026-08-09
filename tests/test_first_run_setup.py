"""First-run auto-wiring of the status line on the plain console report.

`ccmetrics` / `ccmetrics --project=...` wires its own statusLine into
settings.json on its very first console run, announces it in one line, and
never touches it again. Every path is exercised through
`plan.default_settings_path` patched to a tmp file so no test ever touches
the real ~/.claude.

The isatty patch is applied inside each test body (not a setup-phase
fixture): capsys swaps in a fresh sys.stdout between the fixture-setup
phase and the test-call phase, so a patch made during setup would be lost."""

from __future__ import annotations

import json
import sys

import pytest

from ccmetrics import plan, store
from ccmetrics.cli import main


def _make_tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: value)


@pytest.fixture(autouse=True)
def _settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(plan, "default_settings_path", lambda: settings)
    # Pin the resolved invocation so assertions on settings.json content are
    # not at the mercy of how the test runner itself was launched.
    monkeypatch.setattr(plan, "resolve_invocation", lambda: ("ccmetrics", None))
    return settings


def test_non_tty_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, False)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_json_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--json"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_env_var_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, True)
    monkeypatch.setenv("CCMETRICS_NO_SETUP", "1")
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_no_setup_flag_stays_silent(cc_env, capsys, monkeypatch, _settings):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest", "--no-setup"]) == 0
    out = capsys.readouterr().out
    assert "wired its status line" not in out
    assert not _settings.exists()


def test_happy_path_wires_once_and_prints(cc_env, capsys, monkeypatch, conn, _settings):
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert (
        "ccmetrics wired its status line into ~/.claude/settings.json — "
        "your plan usage (5h and weekly %) now shows there." in out
    )
    assert "undo any time: ccmetrics setup --revert" in out
    # nothing was displaced, so the user must not be told anything was
    assert "your old command:" not in out
    assert "that command was:" not in out
    assert _settings.exists()
    written = json.loads(_settings.read_text())
    assert written["statusLine"] == {"type": "command", "command": "ccmetrics statusline",
                                      "refreshInterval": 5}
    assert store.get_meta(conn, "statusline_autowire") is not None

    # second run: meta already set -- silent, plan.apply_setup not called again
    calls = []
    monkeypatch.setattr(plan, "apply_setup", lambda *a, **k: calls.append(1) or {"changed": False})
    written_at = _settings.read_text()
    _make_tty(monkeypatch, True)
    assert main(["--no-ingest"]) == 0
    out2 = capsys.readouterr().out
    assert "wired its status line" not in out2
    assert _settings.read_text() == written_at
    assert calls == []


def test_replaces_an_existing_statusline_and_leaves_a_backup(cc_env, capsys, monkeypatch, conn, _settings):
    _make_tty(monkeypatch, True)
    original = {"statusLine": {"type": "command", "command": "my-other-tool"}, "keepMe": True}
    _settings.write_text(json.dumps(original))

    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "ccmetrics wired its status line into ~/.claude/settings.json" in out

    written = json.loads(_settings.read_text())
    assert written["statusLine"]["command"] == "ccmetrics statusline"
    assert written["keepMe"] is True  # unrelated keys survive

    backup = _settings.with_name(_settings.name + ".bak-ccmetrics")
    assert backup.exists()
    assert json.loads(backup.read_text()) == original

    # the user's own status line just vanished -- they have to be told what it
    # was and where it went
    assert "replacing the status line command that was already there." in out
    assert "your old command: my-other-tool" in out
    assert f"your old command is saved in {backup}" in out


def test_upgrade_from_a_chaining_build_says_what_it_stopped_running(
    cc_env, capsys, monkeypatch, conn, _settings
):
    """An older ccmetrics chained the user's command via --passthrough. First
    run now drops that chain, so the message is about what stopped running --
    not about a command ccmetrics is 'replacing'."""
    _make_tty(monkeypatch, True)
    _settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "ccmetrics statusline --passthrough 'my-tool'"}})
    )

    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out

    assert "ccmetrics stopped running the status line command it was chaining." in out
    assert "that command was: my-tool" in out
    assert "undo any time: ccmetrics setup --revert" in out
    # the displaced command was ours, not the user's -- the foreign-command
    # wording would be a lie here
    assert "your old command:" not in out
    # and the raw wiring is an implementation detail the user never typed
    assert "--passthrough" not in out


def test_a_passthrough_flag_with_no_command_says_nothing(cc_env, capsys, monkeypatch, conn, _settings):
    """`--passthrough` with nothing after it is already wired as far as
    apply_setup is concerned, so first run stays silent. Pinned because the
    CLI works out the chained command a second time, on its own: if that
    early return ever moves, this branch starts telling the user their old
    command was ccmetrics' own.

    The command itself never moves here, but apply_setup still adds a
    missing refreshInterval (item 5) -- that alone must not break the
    silence (cli._first_run_statusline only speaks when `old != new`)."""
    _make_tty(monkeypatch, True)
    original = json.dumps({"statusLine": {"type": "command", "command": "ccmetrics statusline --passthrough"}})
    _settings.write_text(original)

    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out

    assert "wired its status line" not in out
    assert "stopped running" not in out
    assert "your old command:" not in out
    written = json.loads(_settings.read_text())
    assert written["statusLine"]["command"] == "ccmetrics statusline --passthrough"
    assert written["statusLine"]["refreshInterval"] == 5


def test_upgrade_names_the_backup_by_path_without_claiming_its_contents(
    cc_env, capsys, monkeypatch, conn, _settings
):
    """When a usable backup already exists, unwrapping leaves it alone -- so it
    holds whatever was displaced back then, not the command we just stopped
    chaining. The message may point at the file; it must not say what is in
    it."""
    _make_tty(monkeypatch, True)
    _settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "ccmetrics statusline --passthrough 'my-tool'"}})
    )
    backup = _settings.with_name(_settings.name + ".bak-ccmetrics")
    backup.write_text(json.dumps({"statusLine": {"type": "command", "command": "some-older-tool"}}))

    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out

    assert f"the backup next to settings.json is {backup}" in out
    # this is exactly why the claim stays a location: the file does not hold
    # the command we just stopped chaining
    assert json.loads(backup.read_text())["statusLine"]["command"] == "some-older-tool"
    assert "your old command is saved in" not in out


def test_setup_error_path_prints_one_line_and_marks_meta(cc_env, capsys, monkeypatch, conn, _settings):
    _make_tty(monkeypatch, True)
    _settings.write_text("{not json")
    assert main(["--no-ingest"]) == 0
    out = capsys.readouterr().out
    assert "ccmetrics left your status line alone:" in out
    assert "wired its status line" not in out
    assert store.get_meta(conn, "statusline_autowire") is not None
