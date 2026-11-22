"""A log file can never kill the runner, and one runner per state dir (a pid lock)."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import pytest

import ultron_worker.__main__ as cli
from ultron_worker import runlock, sharedfile
from ultron_worker.desktop.autostart import Autostart
from ultron_worker.desktop.controller import App, tail
from ultron_worker.desktop.layout import Install
from ultron_worker.desktop.supervisor import LOG_LINE, RunnerProcess


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cli, "LOG_OPEN_RETRIES", (0.0, 0.0))
    yield
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)  # close our files


def write_config(tmp_path: Path, state: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[backbone]\nurl = "http://127.0.0.1:9"\n'
        f"[runner]\nstate_dir = {json.dumps(str(state))}\n"
        f"allowed_roots = [{json.dumps(str(tmp_path))}]\n"
    )
    return cfg


class FakeRunner:
    ticks = 0

    def __init__(self, cfg, background: bool = True) -> None:  # type: ignore[no-untyped-def]
        pass

    def tick(self) -> None:
        FakeRunner.ticks += 1


def run_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Path) -> int:
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "test-key")
    monkeypatch.setattr(cli, "Runner", FakeRunner)
    FakeRunner.ticks = 0
    return cli.main(["--config", str(write_config(tmp_path, state)), "--once"])


def test_an_unopenable_log_falls_back_to_stderr_and_the_runner_keeps_running(
    tmp_path, monkeypatch, capsys
) -> None:
    state = tmp_path / "state"
    (state / "ultron.log").mkdir(parents=True)  # opening it for append fails on every OS
    assert run_once(tmp_path, monkeypatch, state) == 0
    assert FakeRunner.ticks == 1  # it ran
    err = capsys.readouterr().err
    assert "logging to stderr only, the runner keeps running" in err
    lines = [ln for ln in err.splitlines() if "ultron-runner" in ln or "stderr only" in ln]
    assert lines and all(ln.startswith("[no log file] ") for ln in lines)
    assert not any(LOG_LINE.match(ln) for ln in lines)  # so the app's log pane shows them


def test_a_permission_error_on_the_log_is_retried_then_survived(tmp_path, monkeypatch) -> None:
    real = cli.SharedRotatingFileHandler
    calls = []

    def flaky(*a, **k):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(13, "Permission denied", str(a[0]))
        return real(*a, **k)

    monkeypatch.setattr(cli, "SharedRotatingFileHandler", flaky)
    assert cli._logging(tmp_path) is None  # third try opened it
    logging.getLogger("ultron").info("hello file")
    assert "hello file" in (tmp_path / "ultron.log").read_text()

    def denied(*a, **k):  # type: ignore[no-untyped-def]
        raise PermissionError(13, "Permission denied", str(a[0]))

    monkeypatch.setattr(cli, "SharedRotatingFileHandler", denied)
    problem = cli._logging(tmp_path)
    assert problem and "PermissionError" in problem and "ultron.log" in problem


def test_a_blocked_rotation_keeps_logging(tmp_path, monkeypatch) -> None:
    def in_use(*a, **k):  # type: ignore[no-untyped-def]
        raise PermissionError(13, "in use by another process")

    h = cli.SharedRotatingFileHandler(tmp_path / "ultron.log", maxBytes=200, backupCount=2)
    h.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(os, "rename", in_use)
    monkeypatch.setattr(os, "replace", in_use)
    for i in range(50):
        h.emit(logging.LogRecord("t", logging.INFO, "", 0, f"line {i:02d}", None, None))
    h.close()
    text = (tmp_path / "ultron.log").read_text()
    assert "line 00" in text and "line 49" in text  # nothing lost, rotation retried later


def test_check_does_not_open_the_log_file(tmp_path) -> None:
    assert cli._logging(tmp_path, to_file=False) is None
    assert not (tmp_path / "ultron.log").exists()


def test_tail_and_shared_open(tmp_path) -> None:
    path = tmp_path / "ultron.log"
    with sharedfile.open_shared(path, "a", encoding="utf-8") as fh:
        fh.write("one\ntwo\n")
        fh.flush()
        assert tail(path) == ["one", "two"]  # read while the writer still has it open
    with pytest.raises(ValueError):
        sharedfile.open_shared(path, "w")
    assert sharedfile.FILE_SHARE_ALL == 0x7  # read | write | delete
    assert sharedfile.win_flags("rb")[1] == 3 and sharedfile.win_flags("a")[1] == 4


def test_the_runner_lock_refuses_a_second_runner_cleanly(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    first = runlock.RunnerLock(state)
    assert first.acquire()
    assert runlock.holder(state) == os.getpid()
    monkeypatch.setattr(runlock.RunnerLock, "acquire", lambda self, wait=0.0: False)
    assert run_once(tmp_path, monkeypatch, state) == runlock.ALREADY_RUNNING
    assert FakeRunner.ticks == 0
    err = capsys.readouterr().err
    assert f"already running: another ultron-runner (pid {os.getpid()})" in err
    first.release()
    assert runlock.holder(state) is None  # the file stays, the lock doesn't: not stale
    assert runlock.holder(tmp_path / "nowhere") is None


def test_the_lock_is_held_while_running_and_freed_after(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    seen = []
    monkeypatch.setattr(FakeRunner, "tick", lambda self: seen.append(runlock.holder(state)))
    assert run_once(tmp_path, monkeypatch, state) == 0
    assert seen == [os.getpid()] and runlock.holder(state) is None
    second = runlock.RunnerLock(state)
    assert second.acquire() and not runlock.RunnerLock(state).acquire(wait=0.3)
    second.release()


def test_an_unusable_lock_file_does_not_stop_the_runner(tmp_path, monkeypatch, capsys) -> None:
    state = tmp_path / "state"
    (state / runlock.LOCK_NAME).mkdir(parents=True)
    assert run_once(tmp_path, monkeypatch, state) == 0 and FakeRunner.ticks == 1
    assert "running without it" in capsys.readouterr().out


def test_already_running_is_not_retried(tmp_path) -> None:
    code = "import sys; print('already running: pid 1', file=sys.stderr); sys.exit(4)"
    proc = RunnerProcess(
        lambda: [sys.executable, "-c", code], final_codes=frozenset({runlock.ALREADY_RUNNING})
    )
    proc.start()
    proc._proc.wait(10)  # type: ignore[union-attr]
    proc.poll()
    st = proc.status()
    assert st["state"] == "failed" and st["exit_code"] == 4 and proc.crashes == 0
    assert any("already running" in ln for ln in proc.output())


def test_the_app_reports_a_live_runner_instead_of_spawning_a_rival(tmp_path) -> None:
    state = tmp_path / "state"
    write_config(tmp_path, state)
    app = App(Install(tmp_path, config=tmp_path / "config.toml"), autostart=Autostart(["x"], None))
    app.runner_state = lambda: None  # type: ignore[method-assign]
    spawned = []
    app.runner.start = lambda: spawned.append(1) or True  # type: ignore[method-assign]
    other = runlock.RunnerLock(state)
    assert other.acquire()
    res = app.start_runner()
    assert res["ok"] is False and f"(pid {os.getpid()})" in res["error"] and not spawned
    st = app.status()["runner"]
    assert st["external"] is True and st["external_pid"] == os.getpid()
    other.release()
    assert app.status()["runner"]["external"] is False  # gone: Start works again
    assert app.start_runner()["ok"] is True and spawned == [1]
