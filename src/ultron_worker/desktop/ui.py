"""The native window (pywebview over WebView2) and the tray icon (pystray).

Closing the window hides it to the tray; the runner keeps working. Tray: Open, Restart runner,
Quit (stops the runner cleanly). Without pywebview the control page opens as an Edge app window
(or the default browser), so the app still works if WebView2 can't load.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("ultron.desktop.ui")
TITLE = "Ultron"
APP_ID = "RealWorldDigital.Ultron"


def set_app_id() -> None:
    """Own taskbar identity (not grouped under Python)."""
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)


class DesktopUI:
    def __init__(
        self,
        url: str,
        *,
        background: bool,
        icon_path: Path | None,
        restart_runner: Callable[[], object],
        browser_only: bool = False,
    ) -> None:
        self.url = url
        self.background = background
        self.icon_path = icon_path
        self.restart_runner = restart_runner
        self.browser_only = browser_only
        self.window = None
        self.tray = None
        self.quitting = False
        self._done = threading.Event()

    # ---- public (any thread) ------------------------------------------------
    def show(self) -> None:
        if self.window is None:
            self._open_browser()
            return
        try:
            self.window.show()
            self.window.restore()
            self.window.on_top = True  # bring to front, then let it behave normally
            self.window.on_top = False
        except Exception:  # noqa: BLE001
            log.exception("could not show the window")

    def quit(self) -> None:
        self.quitting = True
        if self.tray is not None:
            with contextlib.suppress(Exception):
                self.tray.stop()
        if self.window is not None:
            with contextlib.suppress(Exception):
                self.window.destroy()
        self._done.set()

    # ---- run (main thread; blocks until Quit) -----------------------------------
    def run(self) -> None:
        set_app_id()
        self._start_tray()
        if not self.browser_only:
            try:
                self._run_webview()
                return
            except Exception:  # noqa: BLE001
                log.exception("native window unavailable; using a browser window instead")
                self.window = None
        if not self.background:
            self._open_browser()
        if self.tray is None:
            log.warning("no tray icon available: Ctrl+C (or the window's Quit) stops Ultron")
        try:
            self._done.wait()
        except KeyboardInterrupt:
            self.quit()

    def _run_webview(self) -> None:
        import webview

        self.window = webview.create_window(
            TITLE,
            self.url,
            width=1320,
            height=900,
            min_size=(960, 640),
            hidden=self.background,
            background_color="#070304",
        )
        self.window.events.closing += self._on_closing
        self.window.events.shown += self._set_window_icon
        webview.start()

    def _on_closing(self) -> bool:
        if self.quitting or self.tray is None:
            self.quitting = True
            self._done.set()
            return True
        threading.Thread(target=self._hide, daemon=True).start()
        return False  # cancel the close: minimize to tray instead

    def _hide(self) -> None:
        with contextlib.suppress(Exception):
            self.window.hide()  # type: ignore[union-attr]

    def _set_window_icon(self) -> None:
        if os.name != "nt" or not self.icon_path:
            return
        try:
            from System.Drawing import Icon  # type: ignore[import-not-found] (pythonnet)

            self.window.native.Icon = Icon(str(self.icon_path))  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            log.debug("window icon not set", exc_info=True)

    def _start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image

            from .icon import make

            image = Image.open(self.icon_path) if self.icon_path else make(64)
            menu = pystray.Menu(
                pystray.MenuItem("Open Ultron", lambda *_: self.show(), default=True),
                pystray.MenuItem("Restart runner", lambda *_: self._bg(self.restart_runner)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit (stops the runner)", lambda *_: self.quit()),
            )
            self.tray = pystray.Icon("ultron", image, "Ultron runner", menu)
        except Exception:  # noqa: BLE001
            log.warning("tray icon unavailable", exc_info=True)
            self.tray = None
            return
        threading.Thread(target=self.tray.run, name="tray", daemon=True).start()

    @staticmethod
    def _bg(fn: Callable[[], object]) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _open_browser(self) -> None:
        edge = shutil.which("msedge") or next(
            (
                p
                for p in (
                    Path(os.environ.get("PROGRAMFILES(X86)", ""))
                    / "Microsoft/Edge/Application/msedge.exe",
                    Path(os.environ.get("PROGRAMFILES", ""))
                    / "Microsoft/Edge/Application/msedge.exe",
                )
                if os.name == "nt" and p.is_file()
            ),
            None,
        )
        if edge:
            with contextlib.suppress(OSError):
                subprocess.Popen([str(edge), f"--app={self.url}"])
                return
        webbrowser.open(self.url)
