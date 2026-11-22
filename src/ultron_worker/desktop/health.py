"""Health lights from ``ultron-runner --check``: state dir, backbone identity, queue, Ollama.

The check runs as its own short process (exactly what David would type), so the lights show
what the runner itself would see, and a broken config or key turns them red with the reason.
If the check itself can't run (won't start, crashes before reporting, times out), that is one
failure, not three: the report is ``unavailable`` with the reason, and no light claims a
component failed.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .layout import NO_WINDOW, now_iso

LIGHTS = ("state", "backbone", "queue", "ollama")
CHECK_TIMEOUT = 120.0
REPORT_LINE = re.compile(r"\s*(state|backbone|queue|ollama|config|job kinds):")


@dataclass(frozen=True)
class Light:
    state: str  # ok | bad | unknown | busy | unavailable
    label: str
    detail: str = ""


def parse_check(code: int | None, output: str) -> dict[str, Light]:
    """Map ``--check`` output (stdout + stderr) and its exit code onto the lights."""
    lights = {name: Light("unknown", "not checked") for name in LIGHTS}
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("config:"):
            reason = line.split(":", 1)[1].strip()
            return {name: Light("bad", "config error", reason) for name in LIGHTS}
    for line in lines:
        head, _, rest = line.partition(":")
        rest = rest.strip()
        if head == "state":
            if rest.startswith("OK"):
                lights["state"] = Light("ok", "writable", rest)
            else:
                lights["state"] = Light("bad", "not writable", rest.removeprefix("FAILED").strip())
        elif head == "backbone":
            if rest.startswith("OK"):
                m = re.search(r"actor '([^']+)'", rest)
                lights["backbone"] = Light("ok", m.group(1) if m else "OK", rest)
            else:
                lights["backbone"] = Light("bad", "failed", rest.removeprefix("FAILED").strip())
                lights["queue"] = Light("bad", "unreachable", "backbone check failed")
        elif head == "queue":
            m = re.match(r"(\d+)", rest)
            n = int(m.group(1)) if m else None
            label = "unknown" if n is None else f"{n} dispatched"
            lights["queue"] = Light("ok", label, rest)
        elif head == "ollama":
            if rest.startswith("OK"):
                lights["ollama"] = Light("ok", "reachable", rest)
            else:
                lights["ollama"] = Light("bad", "failed", rest.removeprefix("FAILED").strip())
    if code not in (0, None) and all(lt.state != "bad" for lt in lights.values()):
        tail = lines[-1] if lines else f"exit code {code}"
        lights = {
            n: (lt if lt.state == "ok" else Light("bad", "failed", tail))
            for n, lt in lights.items()
        }
    return lights


@dataclass
class HealthReport:
    lights: dict[str, Light]
    ok: bool
    code: int | None
    output: str
    checked_at: str
    unavailable: str | None = None  # why the check itself couldn't run; None if it reported

    def to_json(self) -> dict[str, Any]:
        return {
            "lights": {k: asdict(v) for k, v in self.lights.items()},
            "ok": self.ok,
            "code": self.code,
            "checked_at": self.checked_at,
            "unavailable": self.unavailable,
        }


def unavailable(reason: str, output: str = "", code: int | None = None) -> HealthReport:
    lights = {n: Light("unavailable", "check unavailable", reason) for n in LIGHTS}
    return HealthReport(lights, False, code, output, now_iso(), unavailable=reason)


def assess(code: int | None, output: str) -> HealthReport:
    """A finished ``--check`` run: per-component lights if it reported, else unavailable."""
    report = "\n".join(ln for ln in output.splitlines() if REPORT_LINE.match(ln))
    if code != 0 and not report:
        lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
        why = lines[-1] if lines else "no output"
        return unavailable(f"--check exited {code} before reporting: {why}", output[-2000:], code)
    config = [ln.strip() for ln in report.splitlines() if ln.strip().startswith("config:")]
    if config:  # the config didn't load, so no component was checked: one problem
        return unavailable(f"--check could not load the config: {config[0]}", report, code)
    lights = parse_check(code, report)
    return HealthReport(lights, code == 0, code, report, now_iso())


def run_check(
    command: list[str],
    timeout: float = CHECK_TIMEOUT,
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> HealthReport:
    """Run ``<runner command> --check`` (with the runner's environment); never raises."""
    try:
        proc = subprocess.run(
            [*command, "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return unavailable(f"--check did not finish within {timeout:.0f}s")
    except OSError as exc:
        return unavailable(f"could not run --check: {exc}", str(exc))
    # The runner's log lines go to stdout too; assess keeps only the check's own report lines.
    return assess(proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or ""))
