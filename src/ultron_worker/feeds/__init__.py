"""API feeders of the NAS media inbox: the Discord thread and the Google Drive folder.

Each pulls on its own cadence and on a sync-now command (through the inbox watcher's puller
hook), lands files only in its inbox folder and reports the pull to the backbone feeder
registry (``last_pull_at``, ``items_24h`` / ``items_total``, status). Read-only against both
sources: see :class:`.base.ReadOnlyHttp`.
"""

from __future__ import annotations

import functools
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..backbone import BackboneError, BackboneUnavailable
from ..config import Config
from ..jobs import JobError
from .base import FeedError, FeedState, ReadOnlyHttp, utc_now
from .discord import DiscordFeed
from .gdrive import DriveFeed

if TYPE_CHECKING:
    from ..inbox import InboxWatcher

log = logging.getLogger(__name__)

__all__ = ["DiscordFeed", "DriveFeed", "Feed", "FeedRunner", "build_feeds"]


class Feed(Protocol):
    feeder_id: str
    interval_seconds: float

    def problem(self) -> str | None: ...

    def pull(self, source_dir: Path, state: FeedState, stop: threading.Event) -> int: ...


def build_feeds(cfg: Config, http: ReadOnlyHttp | None = None) -> list[Feed]:
    http = http or ReadOnlyHttp()
    feeds: list[Feed] = []
    if cfg.discord_feed.enabled:
        feeds.append(DiscordFeed(cfg.discord_feed, http))
    if cfg.drive_feed.enabled:
        feeds.append(DriveFeed(cfg.drive_feed, http))
    return feeds


class FeedRunner:
    """Schedules the feeders beside the watcher and hands their pulls to it: a sync-now
    command reaches :meth:`pull` through ``watcher.pullers``; cadence pulls report through the
    watcher's durable outbox, so a backbone outage never loses a report."""

    def __init__(
        self,
        cfg: Config,
        watcher: InboxWatcher,
        feeds: list[Feed] | None = None,
        state: FeedState | None = None,
    ) -> None:
        self.cfg, self.watcher = cfg, watcher
        self.stop = watcher.stop
        self.state = state or FeedState(cfg.state_dir / "feeds.sqlite3")
        self.feeds: dict[str, Feed] = {
            f.feeder_id: f for f in (build_feeds(cfg) if feeds is None else feeds)
        }
        self._locks = {fid: threading.Lock() for fid in self.feeds}
        self._due = dict.fromkeys(self.feeds, 0.0)
        self.last: dict[str, dict[str, Any]] = {}
        for fid in self.feeds:
            watcher.pullers[fid] = functools.partial(self._sync_pull, fid)

    def _source_dir(self, fid: str) -> Path:
        folder = next(f.folder for f in self.cfg.media_inbox.feeders if f.feeder_id == fid)
        return self.watcher._abs(f"{self.cfg.media_inbox.inbox}/{folder}")

    def pull(self, fid: str, source_dir: Path | None = None, trigger: str = "cadence") -> dict:
        """One pull: the registry report, plus ``items`` landed. Never raises for a feed fault."""
        feed = self.feeds[fid]
        with self._locks[fid]:
            started = utc_now().isoformat()
            problem = feed.problem()
            items = 0
            if problem is None:
                try:
                    items = feed.pull(source_dir or self._source_dir(fid), self.state, self.stop)
                except FeedError as exc:
                    problem = str(exc)
                except (JobError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
                    problem = f"{type(exc).__name__}: {exc}"
                    log.exception("feeder %s pull failed", fid)
            day, total = self.state.counts(fid)
        report: dict[str, Any] = {
            "ok": problem is None, "pulled_at": started, "items_24h": day, "items_total": total,
        }  # fmt: skip
        if problem:
            report["error"] = problem[:500]
            log.warning("feeder %s (%s): %s", fid, trigger, problem)
        elif items:
            log.info("feeder %s (%s): landed %d file(s)", fid, trigger, items)
        self.last[fid] = {**report, "items": items, "trigger": trigger}
        return {**report, "items": items}

    def _sync_pull(self, fid: str, source_dir: Path) -> dict:
        """The watcher's puller hook (a sync-now command): pull now, whatever the cadence."""
        report = self.pull(fid, source_dir, trigger="sync")
        self._due[fid] = time.monotonic() + self.feeds[fid].interval_seconds
        self.state.set(fid, "reported", report.get("error") or "")
        if not report["ok"]:
            raise FeedError(report["error"])
        return report

    def _paused(self) -> set[str]:
        """Feeders David paused in the registry: their cadence pulls wait (sync-now still runs)."""
        try:
            records = self.watcher.bb.media_feeders()
        except (BackboneError, BackboneUnavailable) as exc:
            log.debug("feeder registry read failed: %s", exc)
            return set()
        return {
            str(r.get("feeder_id"))
            for r in records
            if r.get("status") == "paused" and r.get("feeder_id") in self.feeds
        }

    def _report(self, fid: str, report: dict) -> None:
        """Queue the cadence report. A standing error (not configured, sign-in expired) is
        reported once, not every cadence: the feeder stays stopped until it changes."""
        error = report.get("error") or ""
        if error and self.state.get(fid, "reported") == error:
            return
        self.state.set(fid, "reported", error)
        body = {k: v for k, v in report.items() if k != "items"}
        self.watcher.db.put(f"pull:{fid}", "media_pull", fid, body)

    def tick(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        due = [fid for fid, at in self._due.items() if now >= at]
        if not due:
            return
        paused = self._paused()
        landed = 0
        for fid in due:
            if self.stop.is_set():
                return
            self._due[fid] = now + self.feeds[fid].interval_seconds
            if fid in paused:
                log.info("feeder %s is paused in the registry: cadence pull skipped", fid)
                continue
            report = self.pull(fid)
            landed += report["items"]
            self._report(fid, report)
        if landed:
            self.watcher.rescan.set()

    def run_forever(self, poll_seconds: float = 15.0) -> None:
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:  # the feeder thread must outlive any single fault
                log.exception("feeder tick failed")
            self.stop.wait(poll_seconds)
