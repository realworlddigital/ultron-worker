"""The shell's own loopback server: the control page under ``/__ultron/`` plus a read-only proxy
of the runner's status GUI at ``/``, so the window shows the existing GUI, unchanged, in a
same-origin frame beside the native controls.

The runner GUI sends ``frame-ancestors 'none'`` / ``X-Frame-Options: DENY``; the proxy relaxes
only that, to ``'self'`` (this origin), and only on its own responses. A browser tab on
127.0.0.1:8765 still can't be framed by anyone. Controls are POSTs that need the per-launch
token embedded in the control page, which other origins can't read; every request must carry
a loopback ``Host`` header (no DNS rebinding).
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Protocol

from ..config import is_loopback_host

log = logging.getLogger("ultron.desktop.server")
PREFIX = "/__ultron"
STATIC = {
    "/": ("shell.html", "text/html; charset=utf-8"),
    "/shell.js": ("shell.js", "text/javascript; charset=utf-8"),
    "/shell.css": ("shell.css", "text/css; charset=utf-8"),
}
SHELL_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'",
}
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date"}
RUNNER_DOWN = (
    b"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=3>"
    b"<body style='background:#070304;color:#a78a8d;font:14px Segoe UI,sans-serif;"
    b"display:grid;place-items:center;height:90vh'>"
    b"<p>The runner's status page isn't up (runner stopped or starting).</p>"
)


class Controller(Protocol):
    def status(self) -> dict[str, Any]: ...
    def act(self, action: str, body: dict[str, Any]) -> dict[str, Any]: ...


def frame_same_origin(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Proxy-side rewrite: allow this origin, and only this origin, to frame the GUI."""
    out = []
    for name, value in headers:
        low = name.lower()
        if low in HOP_HEADERS:
            continue
        if low == "x-frame-options":
            value = "SAMEORIGIN"
        elif low == "content-security-policy":
            value = re.sub(r"frame-ancestors[^;]*", "frame-ancestors 'self'", value)
        out.append((name, value))
    return out


def _static(name: str) -> bytes:
    return resources.files("ultron_worker.desktop").joinpath("static", name).read_bytes()


def make_handler(
    controller: Controller, token: str, gui: tuple[str, int]
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ultron-desktop"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("shell: " + fmt, *args)

        def _send(
            self,
            code: int,
            body: bytes,
            ctype: str,
            headers: list[tuple[str, str]] | None = None,
        ) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers if headers is not None else SHELL_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            return is_loopback_host(host)

        def _token_ok(self) -> bool:
            return secrets.compare_digest(self.headers.get("X-Ultron-Token", ""), token)

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_ok():
                self._send(403, b"localhost only", "text/plain")
                return
            path = self.path.split("?", 1)[0]
            if path == PREFIX:
                self.send_response(302)
                self.send_header("Location", PREFIX + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif path.startswith(PREFIX + "/"):
                self._shell(path[len(PREFIX) :])
            else:
                self._proxy()

        do_HEAD = do_GET  # noqa: N815

        def _shell(self, sub: str) -> None:
            if sub == "/api/status":
                if not self._token_ok():
                    self._send(403, b"token required", "text/plain")
                    return
                self._json(200, controller.status())
            elif sub in STATIC:
                name, ctype = STATIC[sub]
                body = _static(name)
                if name == "shell.html":
                    body = body.replace(b"{{TOKEN}}", token.encode())
                self._send(200, body, ctype)
            else:
                self._send(404, b"not found", "text/plain")

        def _proxy(self) -> None:
            conn = http.client.HTTPConnection(gui[0], gui[1], timeout=5)
            try:
                conn.request("GET", self.path, headers={"Host": f"{gui[0]}:{gui[1]}"})
                resp = conn.getresponse()
                body = resp.read()
            except OSError:
                self._send(
                    502, RUNNER_DOWN, "text/html; charset=utf-8", [("Cache-Control", "no-store")]
                )
                return
            finally:
                conn.close()
            headers = frame_same_origin(resp.getheaders())
            ctype = resp.getheader("Content-Type", "application/octet-stream")
            headers = [(k, v) for k, v in headers if k.lower() != "content-type"]
            self._send(resp.status, body, ctype, headers)

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_ok() or not self._token_ok():
                self._send(403, b"forbidden", "text/plain")
                return
            path = self.path.split("?", 1)[0]
            if not path.startswith(PREFIX + "/api/"):
                self._send(405, b"the runner GUI is display-only", "text/plain")
                return
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 65536)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                result = controller.act(path[len(PREFIX + "/api/") :], body)
            except (ValueError, KeyError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001 (report, don't kill the server thread)
                log.exception("action %s failed", path)
                self._json(500, {"ok": False, "error": str(exc)})
                return
            self._json(200, result)

        def _refuse(self) -> None:
            self._send(405, b"not allowed", "text/plain")

        do_PUT = do_PATCH = do_DELETE = _refuse  # noqa: N815

    return Handler


class ShellServer:
    def __init__(
        self,
        controller: Controller,
        gui: tuple[str, int],
        *,
        port: int = 0,
        token: str | None = None,
    ) -> None:
        self.token = token or secrets.token_urlsafe(24)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", port), make_handler(controller, self.token, gui)
        )
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="shell", daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}{PREFIX}/"

    def start(self) -> ShellServer:
        self.thread.start()
        return self

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
