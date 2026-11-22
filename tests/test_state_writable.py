"""State writability is a startup health check: no card is claimed into a state dir the runner
can't checkpoint, --check reports it as the STATE light, and a DB that refuses a job start
parks the card to needs_david with one plain-language note."""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

import ultron_worker.__main__ as cli
import ultron_worker.runner as runner_mod
from ultron_worker.desktop.health import assess, parse_check
from ultron_worker.state import STATE_UNWRITABLE, Store, state_problem

from .test_log_and_lock import FakeRunner, write_config

ROOT = Path(__file__).resolve().parents[1]
needs_posix_perms = pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="chmod read-only needs a non-root POSIX"
)


@pytest.fixture(autouse=True)
def _quiet_logging():
    yield
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)


def readonly_db(state: Path) -> Path:
    db = state / "ultron.sqlite3"
    Store(db).close()
    for f in state.glob("ultron.sqlite3*"):
        f.chmod(stat.S_IRUSR | stat.S_IRGRP)
    return db


def heal(state: Path) -> None:  # what install.ps1 does on Windows (icacls + clear read-only)
    for f in state.iterdir():
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_a_writable_state_dir_passes_and_leaves_no_probe_file(tmp_path) -> None:
    state = tmp_path / "state"
    assert state_problem(state, state / "ultron.sqlite3") is None
    assert not list(state.glob(".write-probe-*"))
    assert state_problem(state, state / "ultron.sqlite3") is None  # repeatable


@needs_posix_perms
def test_a_readonly_db_is_named_with_the_remedy(tmp_path) -> None:
    state = tmp_path / "state"
    db = readonly_db(state)
    problem = state_problem(state, db)
    assert problem and str(db) in problem and "readonly" in problem
    assert "install.ps1" in problem and "\n" not in problem


def test_a_state_dir_that_is_a_file_is_named(tmp_path) -> None:
    state = tmp_path / "state"
    state.write_text("not a dir")
    problem = state_problem(state, state / "ultron.sqlite3")
    assert problem and problem.startswith(f"{state} is not writable")


@needs_posix_perms
def test_check_reports_state_failed_then_ok_after_the_repair(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    db = readonly_db(state)
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "test-key")
    config = str(write_config(tmp_path, state))
    assert cli.main(["--config", config, "--check"]) != 0
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if ln.startswith("state:")]
    assert line.startswith("state: FAILED") and str(db) in line
    report = assess(1, out)
    assert report.lights["state"].state == "bad" and str(db) in report.lights["state"].detail
    heal(state)
    cli.main(["--config", config, "--check"])
    assert f"state: OK, {state} writable" in capsys.readouterr().out


@needs_posix_perms
def test_normal_start_refuses_before_claiming_anything(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    db = readonly_db(state)
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "test-key")
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    FakeRunner.ticks = 0
    config = str(write_config(tmp_path, state))
    assert cli.main(["--config", config, "--once"]) == STATE_UNWRITABLE
    assert FakeRunner.ticks == 0  # no poll, so no card claimed
    err = capsys.readouterr().err
    assert f"state: FAILED {db}" in err and "Traceback" not in err
    heal(state)
    assert cli.main(["--config", config, "--once"]) == 0 and FakeRunner.ticks == 1


def test_the_app_does_not_restart_a_runner_that_cannot_write_its_state() -> None:
    from ultron_worker.desktop import controller

    src = Path(controller.__file__).read_text(encoding="utf-8")
    assert "STATE_UNWRITABLE}" in src  # in the supervisor's final_codes


def test_unwritable_state_claims_nothing_and_logs_once(make, tmp_path, monkeypatch, caplog):
    runner, bb, _ = make()
    bb.add("t1", inputs=[str(tmp_path)], output=str(tmp_path / "o.jsonl"))
    problem = {"now": f"{tmp_path / 'state'} is not writable (denied); fix: install.ps1"}
    monkeypatch.setattr(runner_mod, "state_problem", lambda *_: problem["now"])
    with caplog.at_level(logging.ERROR, logger="ultron"):
        runner.tick()
        runner.tick()
    assert bb.transitions == []
    assert [r.message for r in caplog.records].count(
        f"state: FAILED {problem['now']}; not claiming cards"
    ) == 1
    assert runner.board.snapshot()["runner"]["state_error"] == problem["now"]
    problem["now"] = None  # repaired
    runner.tick()
    assert bb.transitions[0] == ("t1", "in_progress")
    assert runner.board.snapshot()["runner"]["state_error"] is None


def test_a_refused_job_start_retries_then_parks_with_one_note(make, library, tmp_path, monkeypatch):
    monkeypatch.setattr(runner_mod, "START_RETRY_SECONDS", 0)
    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "o.jsonl"))
    calls = []

    def refuse(*a, **k):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(runner.store, "start_job", refuse)
    runner.tick()
    assert len(calls) == runner.cfg.max_attempts
    assert bb.transitions == [("t1", "in_progress"), ("t1", "needs_david")]
    [(tid, note)] = bb.notes
    assert tid == "t1" and note["kind"] == "action"
    body = note["body"]
    assert "could not save its progress" in body and "readonly" in body
    assert str(runner.cfg.db_path) in body and "install.ps1" in body
    assert not ollama.calls
    runner.tick()  # parked: nothing more happens to it
    assert len(bb.transitions) == 2 and len(bb.notes) == 1


def test_a_checkpoint_the_db_refuses_mid_job_parks_too(make, library, tmp_path, monkeypatch):
    runner, bb, _ = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "o.jsonl"))

    def refuse(*a, **k):  # type: ignore[no-untyped-def]
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(runner.store, "checkpoint", refuse)
    runner.tick()
    assert bb.transitions == [("t1", "in_progress"), ("t1", "needs_david")]
    assert len(bb.notes) == 1 and "readonly" in bb.notes[0][1]["body"]


def test_parse_check_state_light() -> None:
    out = "state: FAILED C:\\ProgramData\\Ultron\\state\\ultron.sqlite3 is not writable\n"
    lights = parse_check(1, out + "backbone: OK, key is actor 'x'\nollama: OK\n")
    assert lights["state"].state == "bad" and "ultron.sqlite3" in lights["state"].detail
    assert lights["backbone"].state == "ok"


def test_install_heals_the_state_tree_on_every_run() -> None:
    ps = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    heal_at = ps.index("& icacls $State /grant")
    assert "(OI)(CI)F" in ps[heal_at:].splitlines()[0] and "/T" in ps[heal_at:].splitlines()[0]
    assert "$_.IsReadOnly = $false" in ps
    assert heal_at < ps.index("& icacls $Key")  # the key lockdown still has the last word
    assert "if (-not (Test-Path" not in ps[ps.index("# Heal") : heal_at]  # unconditional
