"""Runner configuration: a TOML file plus the backbone key, which never lives in the file.

The key is read from ``ULTRON_BACKBONE_KEY`` or from ``backbone.key_file`` (written by
``install.ps1`` from David's own prompt, readable only by his account). Nothing here creates
or rotates a credential.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
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


DISCORD_ID = re.compile(r"^\d{15,25}$")  # a Discord snowflake
DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,100}$")  # a Drive folder id (safe inside a query)


@dataclass(frozen=True)
class DiscordFeedConfig:
    """``[media_feeders.discord]``: one Guild Hall thread -> ``<inbox>/<folder>/<date>/``. The
    bot token is David's: ``ULTRON_DISCORD_TOKEN`` or ``token_file``, read at pull time only."""

    enabled: bool = False
    feeder_id: str = "discord"
    display_name: str = "Discord"
    folder: str = "discord"
    guild_id: str = ""
    thread_id: str = ""
    token_file: str = ""
    interval_seconds: float = 300.0


@dataclass(frozen=True)
class DriveFeedConfig:
    """``[media_feeders.gdrive]``: one Drive folder -> ``<inbox>/<folder>/<date>/``. Auth is
    David's stored Google sign-in (``auth_file``, an authorized-user JSON), read at pull time."""

    enabled: bool = False
    feeder_id: str = "gdrive"
    display_name: str = "Google Drive"
    folder: str = "gdrive"
    folder_id: str = ""
    auth_file: str = ""
    interval_seconds: float = 300.0


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
    discord_feed: DiscordFeedConfig = field(default_factory=DiscordFeedConfig)
    drive_feed: DriveFeedConfig = field(default_factory=DriveFeedConfig)

    def api_feeds(self) -> tuple[DiscordFeedConfig | DriveFeedConfig, ...]:
        return tuple(f for f in (self.discord_feed, self.drive_feed) if f.enabled)

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
    _validate_feeds(cfg)


def _validate_feeds(cfg: Config) -> None:
    """Identity values (thread, folder) may still be blank: the feeder then reports 'not
    configured' to the registry. Only a shape that can never work is a config error."""
    for feed in cfg.api_feeds():
        name = f"media_feeders.{feed.feeder_id}"
        if not cfg.media_inbox.enabled:
            raise ConfigError(f"{name} needs [media_inbox] enabled: it lands files in the inbox")
        listed = {f.feeder_id: f for f in cfg.media_inbox.feeders}.get(feed.feeder_id)
        if listed is None or listed.kind != "api" or listed.folder != feed.folder:
            raise ConfigError(f"{name}: media_inbox.feeders.{feed.feeder_id} must be an api "
                              f"feeder on folder {feed.folder!r}")  # fmt: skip
        if feed.interval_seconds < 30:
            raise ConfigError(f"{name}.interval_seconds must be at least 30")
    d = cfg.discord_feed
    for key in ("guild_id", "thread_id"):
        value = getattr(d, key)
        if d.enabled and value and not DISCORD_ID.fullmatch(value):
            raise ConfigError(f"media_feeders.discord.{key} must be a numeric Discord id")
    g = cfg.drive_feed
    if g.enabled and g.folder_id and not DRIVE_ID.fullmatch(g.folder_id):
        raise ConfigError("media_feeders.gdrive.folder_id must be a Drive folder id")


def _feeds(raw: Any, box: InboxConfig) -> tuple[DiscordFeedConfig, DriveFeedConfig, InboxConfig]:
    """``[media_feeders.*]``, and the inbox feeder each enabled one implies (kind api)."""
    if not isinstance(raw, dict):
        raise ConfigError("[media_feeders] must be a table")
    out: list[Any] = []
    for cls, key, text in (
        (DiscordFeedConfig, "discord", ("guild_id", "thread_id", "token_file")),
        (DriveFeedConfig, "gdrive", ("folder_id", "auth_file")),
    ):
        spec = raw.get(key) or {}
        if not isinstance(spec, dict):
            raise ConfigError(f"media_feeders.{key} must be a table")
        d = cls()
        values: dict[str, Any] = {
            "enabled": bool(spec.get("enabled", False)),
            "display_name": str(spec.get("display_name") or d.display_name)[:80],
            "folder": str(spec.get("folder") or d.folder).strip("/"),
            "interval_seconds": float(spec.get("interval_seconds", d.interval_seconds)),
        }
        for k in text:
            v = str(spec.get(k) or "").strip()
            values[k] = _expand(v) if k.endswith("_file") and v else v
        out.append(cls(**values))
    discord, drive = out
    feeders = list(box.feeders)
    for feed in (discord, drive):
        if feed.enabled and all(f.feeder_id != feed.feeder_id for f in feeders):
            feeders.append(Feeder(feed.feeder_id, "api", feed.folder, feed.display_name))
    return discord, drive, dataclasses.replace(box, feeders=tuple(feeders))


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
    discord, drive, box = _feeds(raw.get("media_feeders", {}), _inbox(raw.get("media_inbox", {})))
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
        media_inbox=box,
        discord_feed=discord,
        drive_feed=drive,
    )
