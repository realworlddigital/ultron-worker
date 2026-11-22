"""The runner as a child process the shell owns: start, stop cleanly, restart, revive on a crash.

Stop closes the child's stdin (``--stop-on-stdin-close``), so the runner pauses at its
checkpoint exactly as it does on Ctrl+C; only if it hasn't exited within ``stop_timeout`` is it
killed, which checkpointing also survives. If the shell itself dies, the pipe closes and the
runner stops with it, so a relaunched shell never finds an orphan runner polling beside its own.

The child's stdout and stderr are captured (and appended to ``output_path``): a runner that dies
before it opens its own log still shows why in the app. A crashed runner is revived with a
backoff a few times; if it keeps crashing the supervisor gives up (``failed``) until Start.
An exit code in ``final_codes`` (another runner already holds the state dir) is not retried.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from .layout import NO_WINDOW, now_iso

log = logging.getLogger("ultron.desktop.runner")
BACKOFF = (5, 15, 45)  # seconds before each revival; after these the supervisor gives up
STABLE_SECONDS = 300.0  # a runner up this long before crashing starts a fresh run of retries
OUTPUT_LINES = 200
OUTPUT_FILE_MAX = 2_000_000
# Lines the runner's logging writes to stdout: they are in ultron.log already.
LOG_LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ")


class RunnerProcess:
    def __init__(
        self,
        command: Callable[[], list[str]],
        *,
        output_path: Path | None = None,
        stop_timeout: float = 90.0,
        cwd: Path | None = None,
        env: Callable[[], dict[str, str]] | None = None,
        final_codes: frozenset[int] = frozenset(),
    ) -> None:
        self.command = command
        self.final_codes = final_codes
        self.output_path = output_path
        self.stop_timeout = stop_timeout
        self.cwd = cwd
        self.env = env
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._out_file: IO[str] | None = None
        self._reader: threading.Thread | None = None
        self._output: deque[str] = deque(maxlen=OUTPUT_LINES)  # the latest run's own output
        self._started_mono = 0.0
        self.gave_up = False
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
            if self.gave_up:  # David pressed Start: a fresh run of retries
                self.gave_up, self.crashes = False, 0
            if self.running():
                return False
            self._spawn()
            return True

    def _spawn(self) -> None:
        cmd = [*self.command(), "--stop-on-stdin-close"]
        self._output.clear()
        self._open_output()
        log.info("starting runner: %s", " ".join(cmd))
        self.started_at, self.exit_code, self.exited_at = now_iso(), None, None
        self._started_mono = time.monotonic()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                env=self.env() if self.env else None,
                creationflags=NO_WINDOW,
            )
        except (OSError, ValueError) as exc:  # the exe or cwd is missing: a crash, with a reason
            self._proc = None
            self._note(f"could not start the runner: {exc}")
            self.exit_code, self.exited_at = None, now_iso()
            self._close_output()
            self._crashed(time.monotonic())
            return
        self._reader = threading.Thread(
            target=self._read, args=(self._proc,), name="runner-output", daemon=True
        )
        self._reader.start()

    def _open_output(self) -> None:
        if not self.output_path:
            return
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            if self.output_path.is_file() and self.output_path.stat().st_size > OUTPUT_FILE_MAX:
                self.output_path.unlink()
            self._out_file = open(self.output_path, "a", encoding="utf-8")  # noqa: SIM115
            self._out_file.write(f"--- runner start {now_iso()}\n")
            self._out_file.flush()
        except OSError as exc:  # the in-app copy still works
            log.warning("can't write runner output to %s: %s", self.output_path, exc)
            self._out_file = None

    def _note(self, line: str) -> None:
        self._output.append(line)
        if self._out_file is not None:
            with contextlib.suppress(OSError, ValueError):
                self._out_file.write(line + "\n")
                self._out_file.flush()
                if self._out_file.tell() > OUTPUT_FILE_MAX:
                    self._rotate_output()

    def _rotate_output(self) -> None:
        """Keep one previous file, so a chatty runner can't fill the disk between restarts."""
        assert self.output_path is not None and self._out_file is not None
        self._out_file.close()
        self._out_file = None
        try:
            os.replace(self.output_path, self.output_path.with_name(self.output_path.name + ".1"))
            mode = "a"
        except OSError:  # e.g. .1 held open on Windows: start this file over instead
            mode = "w"
        try:
            self._out_file = open(self.output_path, mode, encoding="utf-8")  # noqa: SIM115
        except OSError as exc:
            log.warning("can't write runner output to %s: %s", self.output_path, exc)

    def _read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        with contextlib.suppress(OSError, ValueError):
            for raw in iter(proc.stdout.readline, b""):
                self._note(raw.decode("utf-8", "replace").rstrip("\r\n"))

    def _close_output(self) -> None:
        if self._out_file is not None:
            with contextlib.suppress(OSError):
                self._out_file.close()
            self._out_file = None

    def output(self, *, include_log_lines: bool = False) -> list[str]:
        """The latest run's stdout/stderr (tracebacks, startup errors). Log-formatted lines are
        left out unless asked for: the log pane shows them from ultron.log already."""
        lines = list(self._output)
        return lines if include_log_lines else [ln for ln in lines if not LOG_LINE.match(ln)]

    def stop(self, timeout: float | None = None) -> str:
        """Stop and wait. Returns ``"clean"``, ``"killed"`` or ``"not running"``."""
        with self._lock:
            self.wanted = False
            self._revive_at = None
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._reap()
                self.crashes, self.gave_up = 0, False
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
                self.crashes, self.gave_up = 0, False
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
            if self._reader is not None:
                self._reader.join(5)  # the last words a crashing runner wrote
                self._reader = None
        if self._proc is None:
            self._close_output()

    def _crashed(self, now: float) -> None:
        if self.exit_code in self.final_codes:  # retrying can't help: say why, once
            self.gave_up, self._revive_at = True, None
            log.error("runner exited with code %s; not restarting it (see the log)", self.exit_code)
            return
        if time.monotonic() - self._started_mono >= STABLE_SECONDS:
            self.crashes = 0  # it ran fine for a while: this is a new problem
        if self.crashes >= len(BACKOFF):
            self.gave_up, self._revive_at = True, None
            log.error(
                "runner exited with code %s %s times in a row; not restarting it (see the log)",
                self.exit_code,
                self.crashes + 1,
            )
            return
        delay = BACKOFF[self.crashes]
        self.crashes += 1
        self._revive_at = now + delay
        log.error("runner exited with code %s; restarting in %ss", self.exit_code, delay)

    def poll(self, now: float | None = None) -> None:
        """Called periodically: notice a crash and revive the runner with backoff."""
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.stopping:
                return
            if self._proc is not None and self._proc.poll() is not None:
                self._reap()
                if self.wanted:
                    self._crashed(now)
            revive = self._revive_at
            if self.wanted and self._proc is None and revive is not None and now >= revive:
                self._revive_at = None
                self._spawn()

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self.running()
            if running:
                state = "stopping" if self.stopping else "running"
            elif self.wanted and self.gave_up:
                state = "failed"  # kept crashing: not retried until Start
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
                "crashes": self.crashes,
                "max_restarts": len(BACKOFF),
            }
