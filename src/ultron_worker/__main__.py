"""``ultron-runner --config config.toml [--no-gui] [--once] [--check] [--stop-on-stdin-close]``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path

from . import __version__, jobs
from .config import Config, ConfigError, load
from .gui import start_gui
from .runlock import ALREADY_RUNNING, RunnerLock, holder
from .runner import Runner
from .sharedfile import open_shared
from .state import STATE_UNWRITABLE, state_problem

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
# Without a log file the lines go to stderr with this prefix, so the app's log pane (which hides
# plain log-formatted output because it is in ultron.log already) shows them.
STDERR_FORMAT = "[no log file] " + LOG_FORMAT
LOG_OPEN_RETRIES = (0.25, 0.5, 1.0)  # seconds between attempts to open ultron.log


class SharedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Opened share-friendly (see :mod:`.sharedfile`); a rotation another process blocks just
    keeps appending and is tried again a minute later instead of losing records."""

    ROTATE_RETRY = 60.0

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._rotate_after = 0.0
        super().__init__(*args, **kwargs)

    def _open(self):  # type: ignore[no-untyped-def]
        return open_shared(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors)

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        return time.monotonic() >= self._rotate_after and bool(super().shouldRollover(record))

    def doRollover(self) -> None:  # noqa: N802
        try:
            super().doRollover()
        except OSError:
            self._rotate_after = time.monotonic() + self.ROTATE_RETRY
            if self.stream is None:
                self.stream = self._open()


def _log_file(path: Path) -> logging.Handler:
    """ultron.log, retried briefly: another process may hold it for a moment."""
    for delay in (*LOG_OPEN_RETRIES, None):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            return SharedRotatingFileHandler(
                path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
            )
        except OSError:
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def _logging(state_dir: Path, *, to_file: bool = True) -> str | None:
    """Logging to ultron.log and stdout. Best-effort: if the log file can't be opened, log to
    stderr only and return why; never raises, the runner keeps running either way."""
    path = state_dir / "ultron.log"
    problem = None
    handlers: list[logging.Handler] = []
    if to_file:
        try:
            handlers.append(_log_file(path))
        except (OSError, ValueError) as exc:
            problem = f"can't open {path} ({type(exc).__name__}: {exc})"
    if problem is None:
        out = logging.StreamHandler(sys.stdout)
        out.setFormatter(logging.Formatter(LOG_FORMAT))
    else:
        out = logging.StreamHandler(sys.stderr)
        out.setFormatter(logging.Formatter(STDERR_FORMAT))
    for h in handlers:
        h.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, handlers=[*handlers, out], force=True)
    if problem:
        logging.getLogger("ultron").warning(
            "%s; logging to stderr only, the runner keeps running", problem
        )
    return problem


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ultron-runner", description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--no-gui", action="store_true", help="run headless (no localhost GUI)")
    ap.add_argument("--once", action="store_true", help="one poll (runs a job inline), then exit")
    ap.add_argument(
        "--check",
        action="store_true",
        help="validate config, state dir, key identity, Ollama and job kinds, then exit",
    )
    ap.add_argument(
        "--stop-on-stdin-close",
        action="store_true",
        help="stop cleanly (at the checkpoint) when stdin closes: the desktop app owns the runner",
    )
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config)
    except (ConfigError, OSError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    # --check runs beside a live runner (the app's health lights): it neither writes the log
    # file nor takes the runner lock.
    _logging(cfg.state_dir, to_file=not args.check)
    log = logging.getLogger("ultron")
    lock = None if args.check else RunnerLock(cfg.state_dir)
    try:
        if lock is not None and not _acquire(lock, log):
            pid = holder(cfg.state_dir)
            print(
                f"already running: another ultron-runner (pid {pid if pid and pid > 0 else '?'})"
                f" holds {lock.path}; not starting a second one",
                file=sys.stderr,
            )
            return ALREADY_RUNNING
        problem = state_problem(cfg.state_dir, cfg.db_path)
        if args.check:
            print(f"state: FAILED {problem}" if problem else f"state: OK, {cfg.state_dir} writable")
        elif problem:  # before any card is claimed; restarting can't help, a repair can
            print(f"state: FAILED {problem}", file=sys.stderr)
            return STATE_UNWRITABLE
        try:
            runner = Runner(cfg, background=not args.once)
        except (OSError, sqlite3.Error) as exc:  # e.g. a state dir this account can't write
            print(
                f"startup: {type(exc).__name__}: {exc} (state_dir {cfg.state_dir})",
                file=sys.stderr,
            )
            return 3
        if problem:
            runner._set_state_error(problem)
        try:
            return _run(args, cfg, runner, log)
        finally:
            store = getattr(runner, "store", None)
            if store is not None:
                store.close()
    finally:
        if lock is not None:
            lock.release()


def _acquire(lock: RunnerLock, log: logging.Logger) -> bool:
    """False only if a live runner holds the lock. A lock file we can't open (e.g. created by
    an elevated install) must not stop the runner: it runs unlocked, with a warning."""
    try:
        return lock.acquire(wait=5.0)  # a just-killed runner's lock frees a moment later
    except OSError as exc:
        log.warning("can't use the runner lock %s (%s); running without it", lock.path, exc)
        return True


def _run(args: argparse.Namespace, cfg: Config, runner: Runner, log: logging.Logger) -> int:
    log.info("ultron-runner %s as %s; job kinds %s", __version__, cfg.actor, jobs.kinds())
    if args.check:
        return _check(runner, state_ok=runner.state_error is None)
    if args.once:
        runner.tick()
        return 0
    if cfg.gui_enabled and not args.no_gui:
        start_gui(runner.board, cfg.gui_host, cfg.gui_port)

    def _stop(*_: object) -> None:
        log.info("stopping: the current job pauses at its checkpoint")
        runner.stop.set()
        runner.poll_now.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _stop)
    if args.stop_on_stdin_close:
        threading.Thread(target=_stop_on_eof, args=(_stop,), name="stdin", daemon=True).start()
    runner.run_forever()
    return 0


def _stop_on_eof(stop) -> None:  # type: ignore[no-untyped-def]
    """Windows has no console signal for a windowless child: the owner closes our stdin."""
    try:
        while sys.stdin.buffer.read(4096):
            pass
    except (OSError, ValueError, AttributeError):
        pass
    stop()


def _check(runner: Runner, *, state_ok: bool = True) -> int:
    from .backbone import BackboneError, BackboneUnavailable
    from .jobs.transcribe import available

    ok = state_ok
    try:
        runner._verify_identity()
        print(f"backbone: OK, key is actor {runner.cfg.actor!r}")
        print(f"queue: {len(runner.bb.cards('dispatched'))} dispatched card(s)")
    except (BackboneError, BackboneUnavailable) as exc:
        ok = False
        print(f"backbone: FAILED {exc}")
    try:
        runner.ollama._post("/api/show", {"model": "__probe__"})
    except Exception as exc:  # noqa: BLE001
        reachable = "404" in str(exc) or "not found" in str(exc).lower()
        print(f"ollama: {'OK' if reachable else 'FAILED ' + str(exc)}")
        ok = ok and reachable
    print(
        f"job kinds: {', '.join(jobs.kinds())}; faster-whisper "
        f"{'installed' if available() else 'not installed'}"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
