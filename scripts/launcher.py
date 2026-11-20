"""Ultron.exe: the tiny launcher PyInstaller freezes (stdlib only, so it never needs a rebuild
when the app updates). It starts the desktop app from the venv named in install.json, so an
update that switches venvs is picked up on the next launch, and passes its own path so the app
can register it for autostart.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Ultron"
FLAGS = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)


def _error(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "Ultron", 0x10)
    except Exception:  # noqa: BLE001
        print(text, file=sys.stderr)


def main() -> int:
    try:
        data = json.loads((ROOT / "install.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    for venv in (data.get("venv"), data.get("previous_venv"), str(ROOT / "venv")):
        pyw = Path(venv) / "Scripts" / "pythonw.exe" if venv else None
        if pyw and pyw.is_file():
            break
    else:
        _error(f"No Ultron install found under {ROOT}.\nRun scripts\\install.ps1 from the repo.")
        return 1
    cmd = [
        str(pyw),
        "-m",
        "ultron_worker.desktop",
        "--root",
        str(ROOT),
        "--launcher",
        sys.executable,
    ]
    subprocess.Popen([*cmd, *sys.argv[1:]], creationflags=FLAGS, close_fds=True, cwd=str(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
