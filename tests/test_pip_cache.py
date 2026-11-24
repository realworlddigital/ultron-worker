"""A poisoned per-user pip cache must never break an install or update (Errno 13 on a cached
wheel broke the 0.3.0 desktop update)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from ultron_worker.desktop import update
from ultron_worker.desktop.layout import Install

REPO = Path(__file__).resolve().parents[1]

_BACKEND = """
import os, zipfile
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    di = "tinypkg-0.1.0.dist-info"
    files = {
        "tinypkg/__init__.py": "X = 1\\n",
        di + "/METADATA": "Metadata-Version: 2.1\\nName: tinypkg\\nVersion: 0.1.0\\n",
        di + "/WHEEL": "Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\n"
        "Tag: py3-none-any\\n",
    }
    name = "tinypkg-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(os.path.join(wheel_directory, name), "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
        z.writestr(di + "/RECORD", "".join(k + ",,\\n" for k in files) + di + "/RECORD,,\\n")
    return name
"""
_PYPROJECT = """[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]

[project]
name = "tinypkg"
version = "0.1.0"
"""


@pytest.fixture
def links(tmp_path: Path) -> Path:
    """A find-links dir holding one sdist: pip builds it and caches the wheel (like proxy_tools)."""
    src = tmp_path / "src" / "tinypkg-0.1.0"
    (src / "tinypkg").mkdir(parents=True)
    (src / "tinypkg" / "__init__.py").write_text("X = 1\n")
    (src / "backend.py").write_text(_BACKEND)
    (src / "pyproject.toml").write_text(_PYPROJECT)
    out = tmp_path / "links"
    out.mkdir()
    with tarfile.open(out / "tinypkg-0.1.0.tar.gz", "w:gz") as tar:
        tar.add(src, arcname=src.name)
    return out


def _install(cmd: list[str], env: dict[str, str], target: Path) -> subprocess.CompletedProcess:
    return update._run(
        [*cmd, "install", "--no-index", "--find-links", str(target.parent / "links"), "tinypkg"]
        + ["--target", str(target)],
        lambda _line: None,
        300,
        env,
    )


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0, reason="needs chmod to deny reads (POSIX, non-root)"
)
def test_update_pip_ignores_a_poisoned_cache(
    tmp_path: Path, links: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache"
    monkeypatch.setenv("PIP_CACHE_DIR", str(cache))  # stands in for %LocalAppData%\pip\cache
    plain_env = {**os.environ, "PIP_CACHE_DIR": str(cache)}
    plain = [sys.executable, "-m", "pip"]
    assert _install(plain, plain_env, tmp_path / "t0").returncode == 0
    cached = list(cache.rglob("tinypkg-*.whl"))
    assert cached, "pip should have cached the built wheel"
    for whl in cached:
        whl.chmod(0)
    try:
        poisoned = _install(plain, plain_env, tmp_path / "t1")
        assert poisoned.returncode != 0 and "Permission denied" in (
            poisoned.stdout + poisoned.stderr
        ), "the planted file should break a cache-using pip"

        proc = _install(update.pip(sys.executable), update.pip_env(), tmp_path / "t2")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (tmp_path / "t2" / "tinypkg" / "__init__.py").exists()
    finally:
        for whl in cached:
            whl.chmod(0o644)


def test_prepare_runs_every_pip_step_without_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "install.json").write_text(f'{{"repo": "{repo.as_posix()}"}}')
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def run(cmd, log, timeout, env=None):  # noqa: ANN001
        calls.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(update, "pull", lambda *_: None)
    monkeypatch.setattr(update, "_run", run)
    target = update.Commit("b" * 40, "x")
    update.prepare(Install(tmp_path), target, log=lambda _l: None, base_python=sys.executable)
    pips = [(cmd, env) for cmd, env in calls if cmd[1:3] == ["-m", "pip"]]
    assert len(pips) == 2
    for cmd, env in pips:
        assert cmd[3] == "--no-cache-dir"
        assert env is not None and env["PIP_NO_CACHE_DIR"] == "1"


def test_install_ps1_pip_calls_skip_the_cache() -> None:
    text = (REPO / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert re.search(r'^\$env:PIP_NO_CACHE_DIR = "1"', text, re.M)
    calls = re.findall(r"-m pip .*", text)
    assert len(calls) >= 3
    assert all(c.startswith("-m pip --no-cache-dir ") for c in calls), calls
