"""One Ultron app per login: the first launch holds a loopback port; a second launch tells it
to show its window and exits."""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable

PORT = 8764
SHOW = b"ultron:show\n"


class SingleInstance:
    def __init__(self, port: int = PORT, host: str = "127.0.0.1") -> None:
        self.host, self.port = host, port
        self.sock: socket.socket | None = None

    def acquire(self, wait: float = 0.0) -> bool:
        deadline = time.monotonic() + wait
        while True:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: nobody else may share it
                s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:  # POSIX: still one listener, but TIME_WAIT doesn't block a relaunch
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.host, self.port))
                s.listen(4)
            except OSError:
                s.close()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.5)
                continue
            self.sock = s
            return True

    def serve(self, on_show: Callable[[], None]) -> None:
        def loop() -> None:
            while self.sock is not None:
                try:
                    conn, _ = self.sock.accept()
                except OSError:
                    return
                with conn:
                    conn.settimeout(2)
                    with contextlib.suppress(OSError):
                        data = b""
                        while chunk := conn.recv(64):  # the client closes first (no TIME_WAIT here)
                            data += chunk
                        if data.startswith(SHOW.strip()):
                            on_show()

        threading.Thread(target=loop, name="single-instance", daemon=True).start()

    def notify_existing(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=3) as s:
                s.sendall(SHOW)
            return True
        except OSError:
            return False

    def release(self) -> None:
        s, self.sock = self.sock, None
        if s is not None:
            with contextlib.suppress(OSError):
                s.shutdown(socket.SHUT_RDWR)  # wake the accept() thread so the port frees now
            s.close()
