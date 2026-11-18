"""Runner configuration: a TOML file plus the backbone key, which never lives in the file.

The key is read from ``ULTRON_BACKBONE_KEY`` or from ``backbone.key_file`` (written by
``install.ps1`` from David's own prompt, readable only by his account). Nothing here creates
or rotates a credential.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# The card contract (docs/backbone-contract.md). Gilfoyle's intake only routes task.dev_build,
# and the runner only takes this type with this executor.
TASK_TYPE = "task.local_job"
EXECUTOR = "local-ultron"
DEFAULT_DOMAIN = "compute"
# Hard exclusion: the runner never reads or writes these, whatever the key would allow.
FORBIDDEN_DOMAINS = frozenset({"care", "household", "finance", "personal"})
EVENT_PREFIX = "ultron."


class ConfigError(ValueError):
    pass


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _expand(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


@dataclass(frozen=True)
class Config:
    backbone_url: str
    key: str
    actor: str = "ultron-runner"
    domain: str = DEFAULT_DOMAIN
    poll_seconds: float = 60.0
    heartbeat_seconds: float = 60.0
    progress_seconds: float = 60.0
    max_attempts: int = 3
    state_dir: Path = Path(".ultron")
    allowed_roots: tuple[Path, ...] = ()
    job_modules: tuple[str, ...] = ()
    ollama_url: str = "http://127.0.0.1:11434"
    gui_enabled: bool = True
    gui_port: int = 8765
    gui_host: str = "127.0.0.1"
    sleep_gap_seconds: float = 90.0
    host_name: str = field(default_factory=lambda: os.environ.get("COMPUTERNAME", "ultron"))

    def __post_init__(self) -> None:
        validate(self)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "ultron.sqlite3"


def validate(cfg: Config) -> None:
    url = urlparse(cfg.backbone_url)
    if url.scheme != "https" and not is_loopback_host(url.hostname):
        raise ConfigError("backbone.url must be https (plain http only for a loopback test API)")
    if not cfg.key:
        raise ConfigError("no backbone key: set ULTRON_BACKBONE_KEY or backbone.key_file")
    if cfg.domain in FORBIDDEN_DOMAINS:
        raise ConfigError(f"domain {cfg.domain!r} is a hard-excluded domain")
    if not is_loopback_host(urlparse(cfg.ollama_url).hostname):
        raise ConfigError("ollama.url must be a localhost address: Ollama stays localhost-only")
    if not is_loopback_host(cfg.gui_host):
        raise ConfigError("gui.host must be a localhost address: the GUI is never on the LAN")
    if cfg.max_attempts < 1:
        raise ConfigError("runner.max_attempts must be at least 1")
    if not cfg.allowed_roots:
        raise ConfigError("runner.allowed_roots must list the folders jobs may read and write")


def _read_key(section: dict) -> str:
    env = os.environ.get("ULTRON_BACKBONE_KEY", "").strip()
    if env:
        return env
    key_file = section.get("key_file")
    if key_file:
        path = Path(_expand(str(key_file)))
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return ""


def load(path: str | os.PathLike[str]) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    bb, run = raw.get("backbone", {}), raw.get("runner", {})
    oll, gui = raw.get("ollama", {}), raw.get("gui", {})
    try:
        backbone_url = str(bb["url"]).rstrip("/")
    except KeyError as exc:
        raise ConfigError("backbone.url is required") from exc
    return Config(
        backbone_url=backbone_url,
        key=_read_key(bb),
        actor=str(bb.get("actor", "ultron-runner")),
        domain=str(bb.get("domain", DEFAULT_DOMAIN)),
        poll_seconds=float(run.get("poll_seconds", 60)),
        heartbeat_seconds=float(run.get("heartbeat_seconds", 60)),
        progress_seconds=float(run.get("progress_seconds", 60)),
        max_attempts=int(run.get("max_attempts", 3)),
        sleep_gap_seconds=float(run.get("sleep_gap_seconds", 90)),
        state_dir=Path(_expand(str(run.get("state_dir", ".ultron")))),
        allowed_roots=tuple(Path(_expand(str(p))) for p in run.get("allowed_roots", [])),
        job_modules=tuple(str(m) for m in run.get("job_modules", [])),
        ollama_url=str(oll.get("url", "http://127.0.0.1:11434")).rstrip("/"),
        gui_enabled=bool(gui.get("enabled", True)),
        gui_port=int(gui.get("port", 8765)),
        gui_host=str(gui.get("host", "127.0.0.1")),
    )
