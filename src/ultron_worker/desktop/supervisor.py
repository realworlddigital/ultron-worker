"""The runner as a child process the shell owns: start, stop cleanly, restart, revive on a crash.

Stop closes the child's stdin (``--stop-on-stdin-close``), so the runner pauses at its
checkpoint exactly as it does on Ctrl+C; only if it hasn't exited within ``stop_timeout`` is it
killed, which checkpointing also survives. If the shell itself dies, the pipe closes and the
runner stops with it, so a relaunched shell never finds an orphan runner polling beside its own.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from .layout import NO_WINDOW, now_iso

log = logging.getLogger("ultron.desktop.runner")
BACKOFF = (5, 15, 30, 60, 120, 300)  # seconds before reviving a crashed runner, then capped


class RunnerProcess:
    def __init__(
        self,
        command: Callable[[], list[str]],
        *,
        stderr_path: Path | None = None,
        stop_timeout: float = 90.0,
        cwd: Path | None = None,
    ) -> None:
        self.command = command
        self.stderr_path = stderr_path
        self.stop_timeout = stop_timeout
        self.cwd = cwd
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._stderr: IO[bytes] | None = None
        self.wanted = False  # David's intent: should a runner be up?
        self.started_at: str | None = None
        self.exit_code: int | None = None
        self.exited_at: str | None = None
        self.crashes = 0
        self._revive_at: float | None = None
        self.stopping = False

    @property
    def pid(self) -> int | None:
        p = self._proc
        return p.pid if p and p.poll() is None else None

    def running(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    def start(self) -> bool:
        with self._lock:
            self.wanted = True
            self._revive_at = None
            if self.running():
                return False
            self._spawn()
            return True

    def _spawn(self) -> None:
        cmd = [*self.command(), "--stop-on-stdin-close"]
        if self.stderr_path:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr = open(self.stderr_path, "ab")  # noqa: SIM115 (kept open for the child)
        log.info("starting runner: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,  # the runner logs to ultron.log already
            stderr=self._stderr or subprocess.DEVNULL,
            cwd=self.cwd,
            creationflags=NO_WINDOW,
        )
        self.started_at, self.exit_code, self.exited_at = now_iso(), None, None

    def stop(self, timeout: float | None = None) -> str:
        """Stop and wait. Returns ``"clean"``, ``"killed"`` or ``"not running"``."""
        with self._lock:
            self.wanted = False
            self._revive_at = None
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._reap()
                return "not running"
            self.stopping = True
            try:
                log.info("stopping runner (pid %s): it pauses at its checkpoint", proc.pid)
                with contextlib.suppress(OSError, ValueError):
                    proc.stdin.close()  # type: ignore[union-attr]
                try:
                    proc.wait(timeout if timeout is not None else self.stop_timeout)
                    how = "clean"
                except subprocess.TimeoutExpired:
                    log.warning("runner did not stop in time; killing it (checkpoint holds)")
                    proc.kill()
                    proc.wait(30)
                    how = "killed"
                self._reap()
                self.crashes = 0
                return how
            finally:
                self.stopping = False

    def restart(self) -> str:
        how = self.stop()
        self.start()
        return how

    def _reap(self) -> None:
        if self._proc is not None and self._proc.poll() is not None:
            self.exit_code, self.exited_at = self._proc.returncode, now_iso()
            self._proc = None
        if self._stderr is not None and self._proc is None:
            with contextlib.suppress(OSError):
                self._stderr.close()
            self._stderr = None

    def poll(self, now: float | None = None) -> None:
        """Called periodically: notice a crash and revive the runner with backoff."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.stopping:
                return
            if self._proc is not None and self._proc.poll() is not None:
                self._reap()
                if self.wanted:
                    delay = BACKOFF[min(self.crashes, len(BACKOFF) - 1)]
                    self.crashes += 1
                    self._revive_at = now + delay
                    log.error(
                        "runner exited with code %s; restarting in %ss", self.exit_code, delay
                    )
            revive = self._revive_at
            if self.wanted and self._proc is None and revive is not None and now >= revive:
                self._revive_at = None
                self._spawn()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self.running()
            if running:
                state = "stopping" if self.stopping else "running"
            elif self.wanted and self._revive_at is not None:
                state = "crashed"
            else:
                state = "stopped"
            revive = max(0, round(self._revive_at - time.monotonic())) if self._revive_at else None
            return {
                "state": state,
                "pid": self.pid,
                "started_at": self.started_at if running else None,
                "exit_code": self.exit_code,
                "exited_at": self.exited_at,
                "restart_in": revive,
            }
