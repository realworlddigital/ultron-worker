"""``ultron-runner --config config.toml [--no-gui] [--once] [--check]``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys

from . import __version__, jobs
from .config import ConfigError, load
from .gui import start_gui
from .runner import Runner


def _logging(state_dir) -> None:  # type: ignore[no-untyped-def]
    state_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file = logging.handlers.RotatingFileHandler(
        state_dir / "ultron.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file.setFormatter(fmt)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[file, out])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ultron-runner", description=__doc__)
    ap.add_argument("--config", required=True)
    ap.add_argument("--no-gui", action="store_true", help="run headless (no localhost GUI)")
    ap.add_argument("--once", action="store_true", help="one poll (runs a job inline), then exit")
    ap.add_argument(
        "--check",
        action="store_true",
        help="validate config, key identity, Ollama and job kinds, then exit",
    )
    args = ap.parse_args(argv)
    try:
        cfg = load(args.config)
    except (ConfigError, OSError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    _logging(cfg.state_dir)
    log = logging.getLogger("ultron")
    runner = Runner(cfg, background=not args.once)
    log.info("ultron-runner %s as %s; job kinds %s", __version__, cfg.actor, jobs.kinds())
    if args.check:
        return _check(runner)
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
    runner.run_forever()
    return 0


def _check(runner: Runner) -> int:
    from .backbone import BackboneError, BackboneUnavailable
    from .jobs.transcribe import available

    ok = True
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
