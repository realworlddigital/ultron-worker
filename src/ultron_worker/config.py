"""Runner configuration: a TOML file plus the backbone key, which never lives in the file.

The key is read from ``ULTRON_BACKBONE_KEY`` or from ``backbone.key_file`` (written by
``install.ps1`` from David's own prompt, readable only by his account). Nothing here creates
or rotates a credential.
"""

from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .media import MediaRoots

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


FEEDER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")  # the backbone's media feeder_id pattern
DEFAULT_TAG_MODEL = "muse-glimmer:30b"  # Glimmer: the runner's local vision default


@dataclass(frozen=True)
class Feeder:
    """One inbox source: ``<media root>/<inbox>/<folder>/``. ``native`` feeders are filled by
    the NAS itself (Synology Photos, Dropbox sync); ``api`` feeders by a registered puller."""

    feeder_id: str
    kind: str = "native"
    folder: str = ""
    display_name: str = ""


@dataclass(frozen=True)
class InboxConfig:
    """``[media_inbox]``: the NAS media-inbox watcher. Off until David confirms the live root."""

    enabled: bool = False
    root: str = ""  # a [media_roots] name, e.g. GUILD-RAW-Media
    inbox: str = "inbox"  # folder under that root holding <source>/<YYYY-MM-DD>/
    watcher: str = "ultron-inbox"
    interval_seconds: float = 300.0
    settle_seconds: float = 60.0  # a file younger than this may still be copying: next pass
    model: str = DEFAULT_TAG_MODEL
    tag_per_pass: int = 50
    review_after_attempts: int = 3
    feeders: tuple[Feeder, ...] = ()
    # subject folder (root-relative) -> tags that file an item there
    subjects: tuple[tuple[str, tuple[str, ...]], ...] = ()


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
    # [media_roots] name -> base path; empty = derived from allowed_roots (see media.py).
    media_roots: tuple[tuple[str, str], ...] = ()
    job_modules: tuple[str, ...] = ()
    ollama_url: str = "http://127.0.0.1:11434"
    gui_enabled: bool = True
    gui_port: int = 8765
    gui_host: str = "127.0.0.1"
    sleep_gap_seconds: float = 90.0
    host_name: str = field(default_factory=lambda: os.environ.get("COMPUTERNAME", "ultron"))
    media_inbox: InboxConfig = field(default_factory=InboxConfig)

    def __post_init__(self) -> None:
        validate(self)

    def roots(self) -> MediaRoots:
        return MediaRoots.build(dict(self.media_roots), self.allowed_roots)

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
    try:
        roots = cfg.roots()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    _validate_inbox(cfg.media_inbox, roots)


def _validate_inbox(box: InboxConfig, roots: MediaRoots) -> None:
    if not box.enabled:
        return
    if roots.lookup(box.root) is None:
        raise ConfigError(f"media_inbox.root {box.root!r} is not a [media_roots] name")
    if not FEEDER_ID.fullmatch(box.watcher):
        raise ConfigError("media_inbox.watcher must be lowercase letters, digits and dashes")
    if not box.model:
        raise ConfigError("media_inbox.model must name the local vision model")
    if box.interval_seconds < 10 or box.tag_per_pass < 0 or box.review_after_attempts < 1:
        raise ConfigError(
            "media_inbox: interval_seconds >= 10, tag_per_pass >= 0, review_after_attempts >= 1"
        )
    rels = [box.inbox, *(f.folder for f in box.feeders), *(s for s, _ in box.subjects)]
    for rel in rels:
        parts = rel.replace("\\", "/").split("/")
        if not rel or rel.startswith(("/", "\\")) or ":" in rel or ".." in parts:
            raise ConfigError(f"media_inbox: {rel!r} must be a relative folder under the root")
    for f in box.feeders:
        if not FEEDER_ID.fullmatch(f.feeder_id) or f.kind not in ("native", "api"):
            raise ConfigError(
                f"media_inbox.feeders.{f.feeder_id}: id must match "
                f"{FEEDER_ID.pattern} and kind be native or api"
            )
    folders = [f.folder.casefold() for f in box.feeders]
    if len(set(folders)) != len(folders):
        raise ConfigError("media_inbox.feeders: two feeders watch the same folder")
    inbox = box.inbox.strip("/").casefold()
    for subject, _ in box.subjects:
        s = subject.strip("/").casefold()
        if s == inbox or s.startswith(inbox + "/") or inbox.startswith(s + "/"):
            raise ConfigError(f"media_inbox.subjects: {subject!r} overlaps the inbox")


def _inbox(raw: dict) -> InboxConfig:
    if not isinstance(raw, dict):
        raise ConfigError("[media_inbox] must be a table")
    feeders = []
    for fid, spec in (raw.get("feeders") or {}).items():
        if not isinstance(spec, dict):
            raise ConfigError(f"media_inbox.feeders.{fid} must be a table")
        feeders.append(
            Feeder(
                feeder_id=str(fid),
                kind=str(spec.get("kind", "native")),
                folder=str(spec.get("folder") or fid).strip("/"),
                display_name=str(spec.get("display_name") or fid)[:80],
            )
        )
    subjects = []
    for folder, tags in (raw.get("subjects") or {}).items():
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise ConfigError(f"media_inbox.subjects.{folder!r} must be a list of tags")
        subjects.append((str(folder).strip("/"), tuple(t.strip().casefold() for t in tags)))
    d = InboxConfig()
    return InboxConfig(
        enabled=bool(raw.get("enabled", False)),
        root=str(raw.get("root", "")),
        inbox=str(raw.get("inbox", d.inbox)).strip("/"),
        watcher=str(raw.get("watcher", d.watcher)),
        interval_seconds=float(raw.get("interval_seconds", d.interval_seconds)),
        settle_seconds=float(raw.get("settle_seconds", d.settle_seconds)),
        model=str(raw.get("model", d.model)),
        tag_per_pass=int(raw.get("tag_per_pass", d.tag_per_pass)),
        review_after_attempts=int(raw.get("review_after_attempts", d.review_after_attempts)),
        feeders=tuple(feeders),
        subjects=tuple(subjects),
    )


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
    media = raw.get("media_roots", {})
    if not isinstance(media, dict) or not all(isinstance(v, str) for v in media.values()):
        raise ConfigError("[media_roots] must map root names to base paths (strings)")
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
        media_roots=tuple((str(k), _expand(v)) for k, v in media.items()),
        job_modules=tuple(str(m) for m in run.get("job_modules", [])),
        ollama_url=str(oll.get("url", "http://127.0.0.1:11434")).rstrip("/"),
        gui_enabled=bool(gui.get("enabled", True)),
        gui_port=int(gui.get("port", 8765)),
        gui_host=str(gui.get("host", "127.0.0.1")),
        media_inbox=_inbox(raw.get("media_inbox", {})),
    )
