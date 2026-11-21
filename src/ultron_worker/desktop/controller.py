"""The app's brain, independent of any window: owns the runner child, the health lights, the
update check and the David-confirmed update, and answers the control page's requests."""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import health, update
from .autostart import Autostart
from .layout import Install, RunnerSettings, now_iso, venv_python
from .supervisor import RunnerProcess

log = logging.getLogger("ultron.desktop")
HEALTH_EVERY = 600.0  # seconds between --check runs (plus on demand and after each start)
IDLE_POLL = 15.0  # while an update waits for the current job to finish
LOG_TAIL_BYTES = 48_000
LOG_TAIL_LINES = 250


def tail(path: Path, max_bytes: int = LOG_TAIL_BYTES, max_lines: int = LOG_TAIL_LINES) -> list[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.decode("utf-8", "replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is probably cut
    return lines[-max_lines:]


def base_python() -> str:
    """The interpreter new venvs are made from (the one this venv was made from)."""
    exe = Path(getattr(sys, "_base_executable", None) or sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").exists():
        exe = exe.parent / "python.exe"
    return str(exe)


class App:
    def __init__(
        self,
        install: Install,
        *,
        autostart: Autostart,
        launcher: Path | None = None,
        updated_to: str | None = None,
        relaunch: Callable[[list[str]], None] | None = None,
    ) -> None:
        self.install = install
        self.settings = RunnerSettings.read(install.config)
        self.autostart = autostart
        self.launcher = launcher
        self.relaunch = relaunch
        self.runner = RunnerProcess(
            install.runner_command,
            stderr_path=self.settings.state_dir / "ultron-runner.stderr.log",
            cwd=install.root if install.root.is_dir() else None,
        )
        self._lock = threading.Lock()
        self.health: health.HealthReport | None = None
        self.checking = False
        self.update_status: update.UpdateStatus | None = None
        self.checking_update = False
        self.job: dict[str, Any] | None = None  # the update in flight, if any
        self._cancel = threading.Event()
        self.flash: dict[str, str] | None = None
        self.external_runner = False
        self._stop = threading.Event()
        self._next_health = 0.0
        if updated_to:
            sha = install.sha or updated_to
            self.flash = {"kind": "ok", "text": f"Updated to {sha[:7]}"}

    # ---- lifecycle ----------------------------------------------------------
    def start(self, *, start_runner: bool = True) -> None:
        if start_runner:
            self.start_runner()
        threading.Thread(target=self._loop, name="monitor", daemon=True).start()
        self.check_updates_async()
        threading.Timer(60, self._prune).start()

    def shutdown(self) -> str:
        self._stop.set()
        self._cancel.set()
        return self.runner.stop()

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            self.runner.poll()
            if time.monotonic() >= self._next_health and not self.checking:
                self.check_health_async()

    def updating(self) -> bool:
        return bool(self.job) and self.job["phase"] in (
            "starting",
            "waiting",
            "building",
            "switching",
        )

    def _prune(self) -> None:
        # Under the lock, so start_update can't begin mid-prune; skipped while an update's new
        # venv (not in install.json yet) is being built.
        with self._lock, contextlib.suppress(OSError):
            if self.updating():
                return
            for path in self.install.prune_venvs():
                log.info("removed old venv %s", path)

    # ---- runner ---------------------------------------------------------------
    def runner_state(self) -> dict[str, Any] | None:
        """The runner's own /api/state, or None if its GUI isn't answering."""
        s = self.settings
        if not s.gui_enabled:
            return None
        conn = http.client.HTTPConnection(s.gui_host, s.gui_port, timeout=1.5)
        try:
            conn.request("GET", "/api/state", headers={"Host": f"{s.gui_host}:{s.gui_port}"})
            resp = conn.getresponse()
            return json.loads(resp.read()) if resp.status == 200 else None
        except (OSError, ValueError):
            return None
        finally:
            conn.close()

    def start_runner(self) -> dict[str, Any]:
        if not self.runner.running() and self.runner_state() is not None:
            self.external_runner = True
            log.warning("a runner started outside the app is serving the status GUI; not starting")
            return {
                "ok": False,
                "error": "A runner started outside the app is already running. Stop it (close "
                "its PowerShell window, or Stop-ScheduledTask 'Ultron Runner'), then press Start.",
            }
        self.external_runner = False
        started = self.runner.start()
        self._next_health = time.monotonic() + 8  # lights refresh shortly after a start
        return {"ok": True, "started": started}

    def busy(self) -> tuple[bool, str]:
        """Is a job running right now? Unknown counts as busy (idle-time update is the default)."""
        if not self.runner.running():
            return False, ""
        st = self.runner_state()
        if st is None:
            return True, "can't read the runner's status page to see whether a job is running"
        cur = st.get("current")
        if cur:
            pct = cur.get("percent")
            return True, f"{cur.get('title') or cur.get('task_id')} ({pct}% done)"
        return False, ""

    # ---- health ---------------------------------------------------------------
    def check_health_async(self) -> None:
        with self._lock:
            if self.checking:
                return
            self.checking = True
        self._next_health = time.monotonic() + HEALTH_EVERY

        def run() -> None:
            try:
                self.health = health.run_check(self.install.runner_command())
            finally:
                self.checking = False

        threading.Thread(target=run, name="health", daemon=True).start()

    # ---- updates ----------------------------------------------------------------
    def check_updates_async(self) -> None:
        with self._lock:
            if self.checking_update:
                return
            self.checking_update = True

        def run() -> None:
            try:
                self.install.reload()
                self.update_status = update.check(self.install.repo, self.install.sha)
            finally:
                self.checking_update = False

        threading.Thread(target=run, name="update-check", daemon=True).start()

    def start_update(self, force: bool) -> dict[str, Any]:
        with self._lock:
            if self.job and self.job["phase"] not in ("failed", "cancelled"):
                if force and self.job["phase"] == "waiting":
                    self.job["force"] = True
                return {"ok": True, "job": self.job}
            st = self.update_status
            if not st or not st.can_update:
                return {"ok": False, "error": "no update to apply: check for updates first"}
            self._cancel.clear()
            self.job = {
                "phase": "starting",
                "target": st.to_json()["available"],
                "log": [],
                "error": None,
                "force": force,
                "waiting_for": None,
                "started_at": now_iso(),
            }
        threading.Thread(
            target=self._update, args=(st.available,), name="update", daemon=True
        ).start()
        return {"ok": True, "job": self.job}

    def _ulog(self, line: str) -> None:
        assert self.job is not None
        self.job["log"] = [*self.job["log"], line][-400:]
        log.info("update: %s", line)

    def _wait_idle(self) -> bool:
        assert self.job is not None
        while not self.job.get("force"):
            busy, what = self.busy()
            if not busy:
                break
            if self.job["phase"] != "waiting":
                self._ulog(f"a job is running: {what}; the update waits until it finishes")
            self.job.update(phase="waiting", waiting_for=what)
            if self._cancel.wait(IDLE_POLL):
                self.job.update(phase="cancelled")
                self._ulog("cancelled; nothing changed")
                return False
        self.job["waiting_for"] = None
        return True

    def _update(self, target: update.Commit) -> None:
        assert self.job is not None
        stopped = False
        try:
            if not self._wait_idle():
                return
            self.job["phase"] = "building"
            new_venv = update.prepare(
                self.install, target, log=self._ulog, base_python=base_python()
            )
            self._ulog(f"--check passed; new install ready in {new_venv}")
            if not self._wait_idle():  # a job may have started while the venv was building
                return
            self.job["phase"] = "switching"
            old = dict(self.install.data)
            how, stopped = self.runner.stop(), True
            self._ulog(f"runner stopped ({how})")
            self.install.save(
                venv=str(new_venv),
                previous_venv=old.get("venv") or str(self.install.venv),
                sha=target.sha,
                updated_at=now_iso(),
            )
            self.autostart.argv = self.autostart_argv()
            with contextlib.suppress(OSError, RuntimeError):
                self.autostart.refresh()
            try:
                self._ulog(f"restarting the app on {target.short}")
                if self.relaunch is None:
                    raise update.UpdateError("this app can't relaunch itself")
                self.relaunch(self.shell_argv(new_venv, updated=target.sha))
                self.job["phase"] = "done"
            except Exception as exc:
                self.install.data = old
                self.install.save()
                raise update.UpdateError(
                    f"relaunch failed ({exc}); back on the previous install"
                ) from exc
        except Exception as exc:  # noqa: BLE001
            self.job.update(phase="failed", error=str(exc))
            self._ulog(f"FAILED: {exc}")
            if stopped and not self._stop.is_set():
                self.start_runner()

    def shell_argv(self, venv: Path | None = None, **extra: str) -> list[str]:
        argv = [
            str(venv_python(venv or self.install.venv, windowed=True)),
            "-m",
            "ultron_worker.desktop",
            "--root",
            str(self.install.root),
            "--config",
            str(self.install.config),
        ]
        if self.launcher:
            argv += ["--launcher", str(self.launcher)]
        for key, value in extra.items():
            argv += [f"--{key.replace('_', '-')}", value]
        return argv

    def autostart_argv(self) -> list[str]:
        if self.launcher and self.launcher.exists():
            return [str(self.launcher), "--background"]
        return [*self.shell_argv(), "--background"]

    # ---- the control page --------------------------------------------------------
    def status(self) -> dict[str, Any]:
        rstate = self.runner_state() if self.runner.running() or self.external_runner else None
        if self.external_runner and rstate is None and not self.runner.running():
            self.external_runner = False  # the outside runner went away: Start works again
        h = self.health
        return {
            "runner": {
                **self.runner.status(),
                "external": self.external_runner,
                "activity": (rstate or {}).get("runner", {}).get("activity"),
                "current": (rstate or {}).get("current"),
            },
            "health": h.to_json() if h else None,
            "checking": self.checking,
            "update": self.update_status.to_json() if self.update_status else None,
            "checking_update": self.checking_update,
            "job": self.job,
            "flash": self.flash,
            "autostart": {
                "supported": self.autostart.supported,
                "enabled": self.autostart.enabled(),
            },
            "installed": {
                "sha": self.install.sha,
                "venv": str(self.install.venv),
                "repo": str(self.install.repo) if self.install.repo else None,
            },
            "log": tail(self.settings.log_path),
            "log_path": str(self.settings.log_path),
        }

    def act(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action == "start":
            return self.start_runner()
        if action == "stop":
            self.external_runner = False
            return {"ok": True, "stopped": self.runner.stop()}
        if action == "restart":
            how = self.runner.stop()
            return {**self.start_runner(), "stopped": how}
        if action == "check":
            self.check_health_async()
            return {"ok": True}
        if action == "update-check":
            self.check_updates_async()
            return {"ok": True}
        if action == "update":
            return self.start_update(bool(body.get("force")))
        if action == "update-cancel":
            if self.job and self.job["phase"] == "waiting":
                self._cancel.set()
            return {"ok": True}
        if action == "autostart":
            self.autostart.argv = self.autostart_argv()
            return {"ok": True, "enabled": self.autostart.set(bool(body.get("enabled")))}
        if action == "dismiss":
            self.flash = None
            if self.job and self.job["phase"] in ("failed", "cancelled"):
                self.job = None
            return {"ok": True}
        raise ValueError(f"unknown action {action!r}")
