"""Where an Ultron install lives, and the ``install.json`` record the app and updater share.

``%ProgramData%\\Ultron``::

    config.toml, runner.key     David's, read exactly as the runner reads them
    install.json                repo checkout, active venv, installed sha, pip extras
    venv\\                      the venv install.ps1 creates
    venvs\\<sha>\\              venvs the updater builds (current + previous are kept)
    ultron-runner.cmd           shim to the active venv's ultron-runner (for --check by hand)
    Ultron.exe, ultron.ico      the launcher (PyInstaller) and its icon

``python -m ultron_worker.desktop.layout record --root R --repo P --venv V --extras desktop``
is what install.ps1 runs to write the record (Python, so the file is plain UTF-8 JSON).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

WINDOWS = os.name == "nt"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def default_root() -> Path:
    return Path(os.environ.get("PROGRAMDATA") or Path.home() / ".ultron") / "Ultron"


def venv_python(venv: Path, *, windowed: bool = False) -> Path:
    if WINDOWS:
        return venv / "Scripts" / ("pythonw.exe" if windowed else "python.exe")
    return venv / "bin" / "python"


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


@dataclass(frozen=True)
class RunnerSettings:
    """The few config.toml values the shell needs. Read without touching the key."""

    state_dir: Path
    gui_enabled: bool
    gui_host: str
    gui_port: int

    @property
    def log_path(self) -> Path:
        return self.state_dir / "ultron.log"

    @classmethod
    def read(cls, config: Path) -> RunnerSettings:
        try:
            with open(config, "rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            raw = {}
        run, gui = raw.get("runner", {}), raw.get("gui", {})
        return cls(
            state_dir=Path(_expand(str(run.get("state_dir", ".ultron")))),
            gui_enabled=bool(gui.get("enabled", True)),
            gui_host=str(gui.get("host", "127.0.0.1")),
            gui_port=int(gui.get("port", 8765)),
        )


class Install:
    def __init__(self, root: Path, config: Path | None = None) -> None:
        self.root = Path(root)
        self.config = Path(config) if config else self.root / "config.toml"
        self.data: dict[str, Any] = {}
        self.reload()

    @property
    def record_path(self) -> Path:
        return self.root / "install.json"

    def reload(self) -> None:
        try:
            # utf-8-sig: tolerate a BOM if anyone edits the file in Windows PowerShell 5.
            self.data = json.loads(self.record_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            self.data = {}

    @property
    def repo(self) -> Path | None:
        return Path(self.data["repo"]) if self.data.get("repo") else None

    @property
    def venv(self) -> Path:
        if self.data.get("venv"):
            return Path(self.data["venv"])
        return Path(sys.prefix)

    @property
    def previous_venv(self) -> Path | None:
        return Path(self.data["previous_venv"]) if self.data.get("previous_venv") else None

    @property
    def sha(self) -> str | None:
        return self.data.get("sha") or None

    @property
    def extras(self) -> list[str]:
        return [str(e) for e in self.data.get("extras", ["desktop"])]

    @property
    def launcher(self) -> Path | None:
        return Path(self.data["launcher"]) if self.data.get("launcher") else None

    def save(self, **changes: Any) -> None:
        self.data = {**self.data, **changes}
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.record_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.record_path)
        self.write_shim()

    def write_shim(self) -> None:
        if not WINDOWS:
            return
        exe = self.venv / "Scripts" / "ultron-runner.exe"
        with contextlib.suppress(OSError):
            (self.root / "ultron-runner.cmd").write_text(f'@"{exe}" %*\r\n', encoding="utf-8")

    def runner_command(self, venv: Path | None = None) -> list[str]:
        return [
            str(venv_python(venv or self.venv)),
            "-m",
            "ultron_worker",
            "--config",
            str(self.config),
        ]

    def prune_venvs(self) -> list[Path]:
        """Delete updater venvs other than the current and previous one (best effort)."""
        keep = {p.resolve() for p in (self.venv, self.previous_venv) if p}
        removed = []
        base = self.root / "venvs"
        for child in sorted(base.iterdir()) if base.is_dir() else []:
            if child.is_dir() and child.resolve() not in keep:
                shutil.rmtree(child, ignore_errors=True)
                if not child.exists():
                    removed.append(child)
        return removed


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git_head(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ultron_worker.desktop.layout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="write install.json after install.ps1 installed a venv")
    rec.add_argument("--root", type=Path, default=default_root())
    rec.add_argument("--repo", type=Path, required=True)
    rec.add_argument("--venv", type=Path, required=True)
    rec.add_argument("--extras", default="desktop")
    rec.add_argument("--launcher", type=Path)
    args = ap.parse_args(argv)
    inst = Install(args.root)
    sha = _git_head(args.repo)
    inst.save(
        repo=str(args.repo.resolve()),
        venv=str(args.venv),
        previous_venv=None,
        sha=sha,
        extras=[e for e in args.extras.split(",") if e],
        launcher=str(args.launcher) if args.launcher else inst.data.get("launcher"),
        installed_at=now_iso(),
    )
    print(f"install.json: {args.venv} at {sha[:7] if sha else 'unknown sha'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
