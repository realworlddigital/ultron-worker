"""The one-click update: waits for idle, never switches on a failed build, rolls back cleanly."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ultron_worker.desktop import controller, update
from ultron_worker.desktop.autostart import Autostart
from ultron_worker.desktop.controller import App
from ultron_worker.desktop.layout import Install

TARGET = update.Commit("b" * 40, "trivial change")


class FakeRunner:
    def __init__(self) -> None:
        self.wanted, self.alive, self.events = True, True, []

    def running(self) -> bool:
        return self.alive

    def start(self) -> bool:
        self.wanted = self.alive = True
        self.events.append("start")
        return True

    def stop(self, timeout: float | None = None) -> str:
        self.wanted = self.alive = False
        self.events.append("stop")
        return "clean"

    def poll(self, now: float | None = None) -> None:
        pass


def wait_for(cond: Callable[[], bool], timeout: float = 5) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


def make_app(tmp_path: Path, relaunch: Callable[[list[str]], None] | None) -> App:
    inst = Install(tmp_path, config=tmp_path / "config.toml")
    inst.save(repo=str(tmp_path / "repo"), venv=str(tmp_path / "venv"), sha="a" * 40)
    app = App(inst, autostart=Autostart(["x"], None), relaunch=relaunch)
    app.runner = FakeRunner()  # type: ignore[assignment]
    app.start_runner = lambda: app.runner.start() and {}  # type: ignore[assignment, method-assign]
    app.update_status = update.UpdateStatus(
        "behind", update.Commit("a" * 40, "old"), TARGET, behind=1
    )
    return app


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[Any]:
    calls: list[Any] = []

    def prepare(install: Install, target: update.Commit, **_: Any) -> Path:
        calls.append(target.sha)
        return tmp_path / "venvs" / target.short

    monkeypatch.setattr(update, "prepare", prepare)
    monkeypatch.setattr(controller, "IDLE_POLL", 0.02)
    return calls


def test_update_switches_and_relaunches_on_the_new_install(tmp_path: Path, built: list) -> None:
    argvs: list[list[str]] = []
    app = make_app(tmp_path, argvs.append)
    app.busy = lambda: (False, "")  # type: ignore[method-assign]
    assert app.start_update(force=False)["ok"]
    wait_for(lambda: app.job["phase"] in ("done", "failed"))
    assert app.job["phase"] == "done", app.job
    assert built == [TARGET.sha] and app.runner.events == ["stop"]
    rec = Install(tmp_path)
    assert rec.sha == TARGET.sha
    assert rec.venv == tmp_path / "venvs" / TARGET.short
    assert rec.previous_venv == tmp_path / "venv"
    assert argvs and "--updated" in argvs[0] and TARGET.sha in argvs[0]


def test_update_waits_while_a_job_runs(tmp_path: Path, built: list) -> None:
    app = make_app(tmp_path, lambda argv: None)
    state = {"busy": True}
    app.busy = lambda: (state["busy"], "Tag photos (40% done)")  # type: ignore[method-assign]
    app.start_update(force=False)
    wait_for(lambda: app.job["phase"] == "waiting")
    time.sleep(0.1)
    assert built == [] and app.runner.events == []  # nothing built or stopped mid-job
    assert app.job["waiting_for"] == "Tag photos (40% done)"
    state["busy"] = False
    wait_for(lambda: app.job["phase"] == "done")


def test_update_now_skips_the_wait(tmp_path: Path, built: list) -> None:
    app = make_app(tmp_path, lambda argv: None)
    app.busy = lambda: (True, "a job")  # type: ignore[method-assign]
    app.start_update(force=False)
    wait_for(lambda: app.job["phase"] == "waiting")
    app.start_update(force=True)
    wait_for(lambda: app.job["phase"] == "done")


def test_cancel_while_waiting_changes_nothing(tmp_path: Path, built: list) -> None:
    app = make_app(tmp_path, lambda argv: None)
    app.busy = lambda: (True, "a job")  # type: ignore[method-assign]
    app.start_update(force=False)
    wait_for(lambda: app.job["phase"] == "waiting")
    app._cancel.set()
    wait_for(lambda: app.job["phase"] == "cancelled")
    assert built == [] and Install(tmp_path).sha == "a" * 40


def test_failed_build_leaves_the_old_install_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, built: list
) -> None:
    def broken(*_: Any, **__: Any) -> Path:
        raise update.UpdateError("--check failed on the new install")

    monkeypatch.setattr(update, "prepare", broken)
    app = make_app(tmp_path, lambda argv: None)
    app.busy = lambda: (False, "")  # type: ignore[method-assign]
    app.start_update(force=False)
    wait_for(lambda: app.job["phase"] == "failed")
    assert "--check failed" in app.job["error"]
    assert app.runner.events == [] and app.runner.alive  # never stopped
    assert Install(tmp_path).venv == tmp_path / "venv"


def test_failed_relaunch_rolls_back_and_restarts_the_runner(tmp_path: Path, built: list) -> None:
    def boom(argv: list[str]) -> None:
        raise OSError("pythonw.exe missing")

    app = make_app(tmp_path, boom)
    app.busy = lambda: (False, "")  # type: ignore[method-assign]
    app.start_update(force=False)
    wait_for(lambda: app.job["phase"] == "failed")
    assert "back on the previous install" in app.job["error"]
    rec = Install(tmp_path)
    assert rec.venv == tmp_path / "venv" and rec.sha == "a" * 40
    assert app.runner.events == ["stop", "start"]


def test_no_update_without_a_check(tmp_path: Path) -> None:
    app = make_app(tmp_path, None)
    app.update_status = update.UpdateStatus("equal")
    assert app.start_update(force=False)["ok"] is False
