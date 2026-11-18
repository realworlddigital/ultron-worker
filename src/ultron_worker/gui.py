"""Localhost status GUI, served by the runner process itself.

A view, not a control plane: GET only (anything else is 405), bound to a loopback address,
and Host-checked so a web page elsewhere can't read it through DNS rebinding. It renders
:meth:`StatusBoard.snapshot`, the same state behind the backbone progress events. The server
runs on a daemon thread; if it can't start, or a browser tab comes and goes, jobs never notice.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from .config import is_loopback_host
from .state import StatusBoard

log = logging.getLogger("ultron.gui")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
    "style-src 'self'; script-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'",
}


def _static(name: str) -> bytes:
    return resources.files("ultron_worker").joinpath("gui", "static", name).read_bytes()


def make_handler(board: StatusBoard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ultron-gui"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet: no access log spam
            log.debug("gui: " + fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return is_loopback_host(host)

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._host_ok():
                self._send(403, b"localhost only", "text/plain")
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                body = json.dumps(board.snapshot()).encode()
                self._send(200, body, "application/json")
            elif path in STATIC:
                name, ctype = STATIC[path]
                self._send(200, _static(name), ctype)
            else:
                self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET  # noqa: N815

        def _refuse(self) -> None:
            self._send(405, b"the Ultron GUI is display-only", "text/plain")

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

    return Handler


class StatusGui:
    def __init__(self, board: StatusBoard, *, host: str = "127.0.0.1", port: int = 8765) -> None:
        if not is_loopback_host(host):
            raise ValueError("the status GUI binds to localhost only")
        self.server = ThreadingHTTPServer((host, port), make_handler(board))
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="gui", daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> StatusGui:
        self.thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def start_gui(board: StatusBoard, host: str, port: int) -> StatusGui | None:
    """Start the GUI, or log why not; the runner works the same either way."""
    try:
        gui = StatusGui(board, host=host, port=port).start()
    except (OSError, ValueError) as exc:
        log.warning("status GUI not started (%s); runner continues headless", exc)
        return None
    log.info("status GUI at %s", gui.url)
    return gui
