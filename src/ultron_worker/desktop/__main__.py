"""``python -m ultron_worker.desktop [--background]``: the Ultron desktop app.

Ultron.exe (the launcher) runs this from the install's venv. A second launch just shows the
running app's window.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import logging.handlers
import os
import subprocess
from pathlib import Path

from .autostart import Autostart, WinRunKey
from .controller import App
from .instance import SingleInstance
from .layout import Install, RunnerSettings, default_root
from .server import ShellServer
from .ui import DesktopUI

log = logging.getLogger("ultron.desktop")
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
)


def _logging(state_dir: Path) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    with contextlib.suppress(OSError):
        state_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            state_dir / "ultron-desktop.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        handlers.append(fh)
    for h in handlers:
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def _icon(root: Path) -> Path | None:
    path = root / "ultron.ico"
    if path.is_file():
        return path
    with contextlib.suppress(Exception):
        from .icon import save_ico

        return save_ico(path)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ultron", description=__doc__)
    ap.add_argument("--root", type=Path, default=default_root(), help="the Ultron install folder")
    ap.add_argument("--config", type=Path, help="config.toml (default: <root>/config.toml)")
    ap.add_argument("--background", action="store_true", help="start in the tray (autostart)")
    ap.add_argument("--launcher", type=Path, help="path of Ultron.exe (set by the launcher)")
    ap.add_argument("--updated", help=argparse.SUPPRESS)  # set by the updater's relaunch
    ap.add_argument("--browser", action="store_true", help="use a browser window, not WebView2")
    ap.add_argument("--no-runner", action="store_true", help="don't start the runner on launch")
    args = ap.parse_args(argv)

    install = Install(args.root, args.config)
    _logging(RunnerSettings.read(install.config).state_dir)
    lock = SingleInstance()
    # After an update the old app may take a moment to exit and free the lock.
    if not lock.acquire(wait=60 if args.updated else 0):
        log.info("Ultron is already running: showing it")
        lock.notify_existing()
        return 0

    launcher = args.launcher or install.launcher
    if (
        launcher
        and launcher.exists()
        and install.data
        and install.data.get("launcher") != str(launcher)
    ):
        install.save(launcher=str(launcher))

    def relaunch(new_argv: list[str]) -> None:
        # The new app waits for this one's single-instance lock, so spawn first, then quit.
        log.info("relaunching: %s", " ".join(new_argv))
        subprocess.Popen(new_argv, creationflags=DETACHED, close_fds=True)
        ui.quit()

    autostart = Autostart([], WinRunKey() if os.name == "nt" else None)
    app = App(
        install, autostart=autostart, launcher=launcher, updated_to=args.updated, relaunch=relaunch
    )
    autostart.argv = app.autostart_argv()
    with contextlib.suppress(OSError):
        autostart.refresh()
    s = app.settings
    server = ShellServer(app, (s.gui_host, s.gui_port)).start()
    log.info("Ultron app (pid %s) at %s", os.getpid(), server.url)
    ui = DesktopUI(
        server.url,
        background=args.background,
        icon_path=_icon(install.root),
        restart_runner=lambda: app.act("restart", {}),
        browser_only=args.browser,
    )
    lock.serve(ui.show)
    app.start(start_runner=not args.no_runner)
    try:
        ui.run()
    finally:
        log.info("quitting: runner %s", app.shutdown())
        server.close()
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
