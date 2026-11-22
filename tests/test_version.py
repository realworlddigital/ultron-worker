"""One version, from pyproject.toml, on every surface David and Apollo look at (no drift)."""

from __future__ import annotations

import argparse
import logging
import re
import tomllib
from pathlib import Path

import pytest

import ultron_worker
from ultron_worker import COMMIT_ENV, __version__
from ultron_worker.__main__ import _run, main

from .test_desktop_update_flow import make_app

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
VERSION = PYPROJECT["version"]


def test_package_version_is_the_pyproject_value() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", VERSION)
    assert __version__ == VERSION


def test_installed_metadata_is_the_fallback_outside_a_clone(tmp_path: Path) -> None:
    assert ultron_worker._version(tmp_path / "missing.toml") == VERSION  # pip built it from there


def test_no_hard_coded_copy_in_the_sources() -> None:
    literal = re.compile(rf"(?<![\d.]){re.escape(VERSION)}(?![\d.])")
    hits = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".js", ".html", ".css"}
        and literal.search(p.read_text(encoding="utf-8"))
    ]
    assert hits == []


def test_changelog_leads_with_the_current_version() -> None:
    first = next(
        ln
        for ln in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
        if ln.startswith("- ")
    )
    assert first.startswith(f"- **{VERSION}**")


def test_gui_heartbeat_log_and_check_all_report_it(make, cfg, caplog, capsys, monkeypatch) -> None:
    monkeypatch.setenv(COMMIT_ENV, "abc1234def" + "0" * 30)
    runner, bb, _ = make()
    snap = runner.board.snapshot()["runner"]
    assert (snap["version"], snap["commit"]) == (VERSION, "abc1234")  # status GUI header

    runner.heartbeat()  # PUT /system/status/ultron-runner
    detail = bb.statuses[-1]["detail"]
    assert (detail["version"], detail["commit"]) == (VERSION, "abc1234")

    with caplog.at_level(logging.INFO, logger="ultron"):
        _run(argparse.Namespace(check=True, once=False), cfg, runner, logging.getLogger("ultron"))
    assert f"ultron-runner {VERSION} (abc1234) as ultron-runner" in caplog.text  # startup line
    assert f"version: ultron-runner {VERSION} (abc1234)" in capsys.readouterr().out  # --check


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0 and capsys.readouterr().out.strip() == f"ultron-runner {VERSION}"


def test_desktop_app_shows_version_beside_the_sha_and_hands_the_sha_to_its_runner(
    tmp_path: Path,
) -> None:
    app = make_app(tmp_path, None)
    app.runner.status = lambda: {"state": "running"}  # type: ignore[attr-defined]
    app.runner_state = lambda: None  # type: ignore[method-assign]
    installed = app.status()["installed"]
    assert (installed["version"], installed["sha"]) == (VERSION, "a" * 40)
    assert app.child_env()[COMMIT_ENV] == "a" * 40
