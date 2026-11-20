"""Health lights from ``ultron-runner --check``: backbone identity, queue, Ollama.

The check runs as its own short process (exactly what David would type), so the lights show
what the runner itself would see, and a broken config or key turns them red with the reason.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

from .layout import NO_WINDOW, now_iso

LIGHTS = ("backbone", "queue", "ollama")
CHECK_TIMEOUT = 120.0


@dataclass(frozen=True)
class Light:
    state: str  # ok | bad | unknown | busy
    label: str
    detail: str = ""


def parse_check(code: int | None, output: str) -> dict[str, Light]:
    """Map ``--check`` output (stdout + stderr) and its exit code onto the three lights."""
    lights = {name: Light("unknown", "not checked") for name in LIGHTS}
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    for line in lines:
        if line.startswith("config:"):
            reason = line.split(":", 1)[1].strip()
            return {name: Light("bad", "config error", reason) for name in LIGHTS}
    for line in lines:
        head, _, rest = line.partition(":")
        rest = rest.strip()
        if head == "backbone":
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

    def to_json(self) -> dict[str, Any]:
        return {
            "lights": {k: asdict(v) for k, v in self.lights.items()},
            "ok": self.ok,
            "code": self.code,
            "checked_at": self.checked_at,
        }


def run_check(command: list[str], timeout: float = CHECK_TIMEOUT) -> HealthReport:
    """Run ``<runner command> --check``; never raises."""
    try:
        proc = subprocess.run(
            [*command, "--check"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        code, output = proc.returncode, (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code, output = None, ""
        lights = {n: Light("bad", "timed out", f"--check took over {timeout:.0f}s") for n in LIGHTS}
        return HealthReport(lights, False, None, output, now_iso())
    except OSError as exc:
        lights = {n: Light("bad", "cannot run", str(exc)) for n in LIGHTS}
        return HealthReport(lights, False, None, str(exc), now_iso())
    # The runner's log lines go to stdout too; keep only the check's own report lines.
    report = "\n".join(
        ln
        for ln in output.splitlines()
        if re.match(r"\s*(backbone|queue|ollama|config|job kinds):", ln)
    )
    lights = parse_check(code, report or output)
    return HealthReport(lights, code == 0, code, report or output.strip()[-2000:], now_iso())
