"""The NAS media-inbox watcher: the single ingest point for ``<root>/<inbox>/<source>/<date>/``.

Each pass, on a cadence and on every backbone sync command naming one of our feeders:

1. inventory: walk every source folder, hash new or changed files (sha256) and record each
   distinct content once; a second arrival of the same bytes is noted on the first record,
   never a second record;
2. tag new items with the local vision model (Glimmer by default) through ``vision_tag``;
3. file each tagged item into the subject folder its tags match (``[media_inbox.subjects]``);
   an item with no usable tags or no single matching subject stays in the inbox, flagged
   for review, never dropped and never silently untagged;
4. upsert the media root's catalog (``_index/catalog.jsonl`` + ``all-tags.jsonl``) from
   ``_index/inbox-tags.jsonl``: root-relative paths, size + mtime, never an absolute path;
5. post the heartbeat (pending photos / videos / bytes per source and in total), arrivals and
   native feeders' inferred last pull to the backbone.

Every backbone write goes through a durable SQLite outbox, coalesced per record, so an outage
or a restart never loses a pass: the newest state is replayed when the backbone is back.
State is checkpointed per file, so a local stop or a hard kill at any point is safe: the next
pass resumes, and a move interrupted mid-way is finished (or redone) before anything else.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .backbone import Backbone, BackboneError, BackboneUnavailable
from .catalog import CATALOG_DIR, file_stamp, update_catalog
from .config import FEEDER_ID, Config, Feeder
from .jobs.base import ItemFailed, JobContext, JobError, JobSpec, WorkItem, write_records
from .jobs.inventory import SYSTEM_DIRS
from .jobs.vision import IMAGE_EXTS, VIDEO_EXTS, VisionTag
from .ollama import OllamaModelMissing, OllamaUnavailable

log = logging.getLogger(__name__)

INDEX_NAME = "inbox-tags.jsonl"  # <root>/_index/inbox-tags.jsonl, listed in the catalog TOC
WORK_DIR = "media-inbox"  # under <state_dir>/jobs/, so the startup scratch sweep covers it
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEMP_SUFFIXES = (".tmp", ".part", ".partial", ".crdownload", ".download", ".filepart")
HASH_CHUNK = 1 << 20
PENDING = ("waiting", "review")  # still in the inbox: what the heartbeat counts
PULL_404_TRIES = 5  # a pull report may race its feeder's registration; then it is refused

# API feeders' pullers, by feeder id: ``fn(source_dir) -> files pulled``. Registered by a
# module listed in runner.job_modules; a native feeder never has one (the NAS syncs it).
PULLERS: dict[str, Callable[[Path], int]] = {}


def register_puller(feeder_id: str) -> Callable[[Callable[[Path], int]], Callable[[Path], int]]:
    def deco(fn: Callable[[Path], int]) -> Callable[[Path], int]:
        PULLERS[feeder_id] = fn
        return fn

    return deco


class InboxError(RuntimeError):
    """The pass couldn't run (inbox missing, outside the allowed roots): reported as error."""


def utc_iso(ts: float | None = None) -> str:
    when = datetime.fromtimestamp(ts, UTC) if ts is not None else datetime.now(UTC)
    return when.isoformat(timespec="seconds")


def slug(name: str) -> str | None:
    """A source folder's inferred feeder id, or None when nothing usable is left."""
    s = re.sub(r"[^a-z0-9-]+", "-", name.casefold()).strip("-")[:48].strip("-")
    return s if FEEDER_ID.fullmatch(s) else None


def sha256(path: Path, stop: threading.Event | None = None) -> str | None:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(HASH_CHUNK):
            if stop is not None and stop.is_set():
                return None
            h.update(chunk)
    return h.hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    hash TEXT PRIMARY KEY, feeder TEXT NOT NULL, rel TEXT NOT NULL, media TEXT NOT NULL,
    bytes INTEGER NOT NULL, src_mtime REAL NOT NULL, arrived_at TEXT NOT NULL,
    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, reason TEXT,
    caption TEXT, tags TEXT, stamp TEXT, dest TEXT, dupes TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS items_rel ON items(rel);
CREATE TABLE IF NOT EXISTS seen (rel TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT);
CREATE TABLE IF NOT EXISTS outbox (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE NOT NULL, op TEXT NOT NULL,
    args TEXT NOT NULL, tries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
# items.status: waiting (untagged) | review (in the inbox, flagged) | filing (move in flight)
#               | filed | missing (gone from the inbox by hand; not counted)


class InboxDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def q(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute(sql, args))

    @contextlib.contextmanager
    def tx(self) -> Iterator[None]:
        """One atomic step: a state change and the outbox row that reports it."""
        with self._lock:
            if self._db.in_transaction:
                yield
                return
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def item(self, h: str) -> dict[str, Any] | None:
        rows = self.q("SELECT * FROM items WHERE hash=?", h)
        return dict(rows[0]) if rows else None

    def update(self, h: str, **fields: Any) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        self.q(f"UPDATE items SET {sets} WHERE hash=?", *fields.values(), h)  # noqa: S608

    def meta(self, key: str) -> str | None:
        rows = self.q("SELECT value FROM meta WHERE key=?", key)
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.q("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", key, value)

    # ---- the durable outbox: one row per record, newest state wins ------------------------
    def put(self, key: str, op: str, *args: Any) -> None:
        with self._lock:
            self._db.execute("DELETE FROM outbox WHERE key=?", (key,))
            self._db.execute(
                "INSERT INTO outbox(key, op, args) VALUES (?, ?, ?)", (key, op, json.dumps(args))
            )

    def queued(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM outbox ORDER BY seq")

    def has(self, key: str) -> bool:
        return bool(self.q("SELECT 1 FROM outbox WHERE key=?", key))

    def done(self, seq: int) -> None:
        self.q("DELETE FROM outbox WHERE seq=?", seq)


@dataclass
class PassResult:
    status: str = "ok"
    error: str | None = None
    new: Counter[str] = field(default_factory=Counter)
    duplicates: int = 0
    tagged: int = 0
    filed: int = 0
    review: int = 0


class InboxWatcher:
    def __init__(
        self,
        cfg: Config,
        bb: Backbone,
        ollama: Any,
        *,
        stop: threading.Event | None = None,
        busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.cfg, self.box, self.bb = cfg, cfg.media_inbox, bb
        self.stop = stop or threading.Event()
        self.busy = busy  # a card job holds the model: inventory and count, tag next pass
        self.roots = cfg.roots()
        self.root = self.roots.lookup(self.box.root) or self.box.root
        self.db = InboxDB(cfg.state_dir / "inbox.sqlite3")
        self.ctx = JobContext(
            task_id="media-inbox",
            ollama=ollama,
            allowed_roots=cfg.allowed_roots,
            work_dir=cfg.state_dir / "jobs" / WORK_DIR,
            stop=self.stop,
            media_roots=self.roots,
        )
        self.spec = JobSpec(
            kind="vision_tag", inputs=(), model=self.box.model, output="", budget_minutes=0
        )
        self.vision = VisionTag()
        self.feeders: dict[str, Feeder] = {f.feeder_id: f for f in self.box.feeders}
        self._by_folder = {f.folder.casefold(): f for f in self.box.feeders}
        self._registered: set[str] = set()
        self.rescan = threading.Event()
        self.last: dict[str, Any] = {}
        self.last_counts: dict[str, int] = {}
        self._pass_lock = threading.Lock()

    # ---- paths -----------------------------------------------------------------------------
    def _abs(self, rel: str) -> Path:
        where = self.roots.resolve(self.root, rel)
        if where is None:
            raise InboxError(f"media root {self.root!r} is not mapped on this machine")
        return self.ctx.allowed(where, what="media inbox path")

    def inbox_dir(self) -> Path:
        base = self._abs(self.box.inbox)
        if not base.is_dir():
            raise InboxError(f"inbox folder {self.root}/{self.box.inbox} not found")
        return base

    # ---- feeders ---------------------------------------------------------------------------
    def feeder_for(self, folder: str) -> Feeder | None:
        """Configured feeder for this source folder, else one inferred from its name."""
        found = self._by_folder.get(folder.casefold())
        if found is not None:
            return found
        fid = slug(folder)
        if fid is None:
            return None
        if fid not in self.feeders:
            self.feeders[fid] = Feeder(feeder_id=fid, kind="native", folder=folder,
                                       display_name=folder[:80])  # fmt: skip
        return self.feeders[fid]

    def register(self) -> None:
        for f in self.feeders.values():
            if f.feeder_id in self._registered:
                continue
            self._registered.add(f.feeder_id)
            definition = {
                "display_name": f.display_name or f.feeder_id,
                "kind": f.kind,
                "source_folder": f"{self.box.inbox}/{f.folder}",
                "worker": self.cfg.actor,
            }
            self.db.put(f"feeder:{f.feeder_id}", "media_feeder", f.feeder_id, definition)

    # ---- one pass --------------------------------------------------------------------------
    def run_pass(self, trigger: str = "cadence") -> PassResult:
        with self._pass_lock:
            return self._run_pass(trigger)

    def _run_pass(self, trigger: str) -> PassResult:
        started = utc_iso()
        res = PassResult()
        dirty = False
        try:
            dirty |= self.reconcile()
            base = self.inbox_dir()
            dirty |= self.inventory(base, res)
            if self.box.tag_per_pass and not self.busy():
                dirty |= self.tag_and_file(res)
            if dirty or not self._index_path().is_file():
                self.write_catalog()
        except (InboxError, JobError, OSError) as exc:
            res.status, res.error = "error", str(exc)
        except (OllamaUnavailable, OllamaModelMissing) as exc:
            res.status, res.error = "error", f"vision model unavailable: {exc}"
            with contextlib.suppress(InboxError, JobError, OSError):
                self.write_catalog()
        self.register()
        self.report(started, res)
        self.last = {"trigger": trigger, "started_at": started, "status": res.status,
                     "error": res.error, "new": sum(res.new.values())}  # fmt: skip
        log.info(
            "inbox pass (%s): %s new, %s duplicate, %s tagged, %s filed, %s for review%s",
            trigger, sum(res.new.values()), res.duplicates, res.tagged, res.filed, res.review,
            f"; error: {res.error}" if res.error else "",
        )  # fmt: skip
        self.flush()
        return res

    # ---- inventory -------------------------------------------------------------------------
    def _files(self, top: Path) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames[:] = sorted(
                d for d in dirnames if d.casefold() not in SYSTEM_DIRS and not d.startswith(".")
            )
            for name in sorted(filenames):
                if (Path(dirpath) / name).is_symlink():
                    continue  # never follow a link out of the allowed roots
                low = name.casefold()
                if name.startswith((".", "~$")) or low.endswith(TEMP_SUFFIXES):
                    continue
                if Path(low).suffix in IMAGE_EXTS | VIDEO_EXTS:
                    yield Path(dirpath) / name

    def inventory(self, base: Path, res: PassResult) -> bool:
        now, present, dirty = time.time(), set(), False
        for src in sorted(base.iterdir(), key=lambda p: p.name):
            skip = src.is_symlink() or src.name.casefold() in SYSTEM_DIRS or src.name[:1] in "._"
            if skip or not src.is_dir():
                continue
            feeder = self.feeder_for(src.name)
            if feeder is None:
                log.warning("inbox: source folder %r has no usable feeder id; skipped", src.name)
                continue
            for path in self._files(src):
                if self.stop.is_set():
                    return dirty
                rel = f"{self.box.inbox}/{path.relative_to(base).as_posix()}"
                try:
                    st = path.stat()
                except OSError:
                    continue
                present.add(rel)
                if now - st.st_mtime < self.box.settle_seconds:
                    continue  # still landing: next pass
                dirty |= self._arrival(path, rel, feeder, st, res)
        if not self.stop.is_set():
            self._vanished(present)
        return dirty

    def _hash(self, path: Path, rel: str, st: os.stat_result) -> str | None:
        mtime = round(st.st_mtime, 3)
        rows = self.db.q("SELECT hash FROM seen WHERE rel=? AND size=? AND mtime=?",
                         rel, st.st_size, mtime)  # fmt: skip
        if rows:
            return rows[0]["hash"]
        h = sha256(path, self.stop)
        if h is not None:
            self.db.q("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)", rel, st.st_size, mtime, h)
        return h

    def _arrival(
        self, path: Path, rel: str, feeder: Feeder, st: os.stat_result, res: PassResult
    ) -> bool:
        try:
            h = self._hash(path, rel, st)
        except OSError as exc:
            log.warning("inbox: can't read %s: %s", rel, exc)
            return False
        if h is None:
            return False
        rec = self.db.item(h)
        media = "video" if path.suffix.lower() in VIDEO_EXTS else "image"
        if rec is None:
            # new bytes at a path a pending item held: that item was replaced, not this one
            with self.db.tx():
                self.db.q("UPDATE items SET status='missing' WHERE rel=? AND hash!=? AND status"
                          " IN ('waiting', 'review')", rel, h)  # fmt: skip
                self.db.q(
                    "INSERT INTO items(hash, feeder, rel, media, bytes, src_mtime, arrived_at,"
                    " status) VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting')",
                    h, feeder.feeder_id, rel, media, st.st_size, st.st_mtime, utc_iso(),
                )  # fmt: skip
                self.report_item(h)
            res.new[feeder.feeder_id] += 1
            return True
        if rec["rel"] == rel:
            if rec["status"] != "missing":
                return False
            self.db.update(h, status="review" if rec["tags"] or rec["reason"] else "waiting")
            return True
        moved = rec["status"] == "missing" or (
            rec["status"] in PENDING and not self._abs(rec["rel"]).exists()
        )
        if moved:  # the same bytes moved within the inbox: the record follows them
            status = "review" if rec["tags"] or rec["reason"] else "waiting"
            with self.db.tx():
                self.db.update(h, rel=rel, feeder=feeder.feeder_id, status=status)
                self.report_item(h)
            return True
        dupes = json.loads(rec["dupes"])
        if all(d["path"] != rel for d in dupes):
            dupes.append({"path": rel, "feeder": feeder.feeder_id, "arrived_at": utc_iso()})
            self.db.update(h, dupes=json.dumps(dupes))
            res.duplicates += 1
            log.info("inbox: %s is a second arrival of %s (same content)", rel, rec["rel"])
            return True
        return False

    def _vanished(self, present: set[str]) -> None:
        for row in self.db.q("SELECT hash, rel FROM items WHERE status IN ('waiting', 'review')"):
            if row["rel"] not in present:
                self.db.update(row["hash"], status="missing")
        stale = [r["rel"] for r in self.db.q("SELECT rel FROM seen") if r["rel"] not in present]
        for rel in stale:
            self.db.q("DELETE FROM seen WHERE rel=?", rel)
        for row in self.db.q("SELECT hash, dupes FROM items WHERE dupes != '[]'"):
            dupes, changed = json.loads(row["dupes"]), False
            for d in dupes:  # a removed second arrival stays noted, stamped gone
                if "gone" not in d and d["path"] not in present:
                    d["gone"], changed = utc_iso(), True
            if changed:
                self.db.update(row["hash"], dupes=json.dumps(dupes))

    # ---- tagging + filing ------------------------------------------------------------------
    def tag_and_file(self, res: PassResult) -> bool:
        rows = self.db.q(
            "SELECT * FROM items WHERE status='waiting' ORDER BY arrived_at, hash LIMIT ?",
            self.box.tag_per_pass,
        )
        dirty = False
        for rec in map(dict, rows):
            if self.stop.is_set():
                break
            dirty = True
            item = WorkItem(
                key=rec["hash"],
                label=rec["rel"],
                payload={"path": str(self._abs(rec["rel"])), "media": rec["media"]},
            )
            try:
                result = self.vision.run_item(item, self.spec, self.ctx)
            except ItemFailed as exc:  # ItemRetry too: an unusable reply after a re-ask
                tries = rec["attempts"] + 1
                if tries >= self.box.review_after_attempts:
                    with self.db.tx():
                        self.db.update(rec["hash"], attempts=tries, status="review",
                                       reason=f"tags could not be inferred: {exc}")  # fmt: skip
                        self.report_item(rec["hash"])
                    res.review += 1
                else:
                    self.db.update(rec["hash"], attempts=tries, reason=None)
                continue
            tags = [str(t) for t in result.get("tags") or []]
            stamp = {k: result[k] for k in ("size_bytes", "mtime") if k in result}
            self.db.update(rec["hash"], caption=result.get("caption"), tags=json.dumps(tags),
                           stamp=json.dumps(stamp), attempts=rec["attempts"] + 1)  # fmt: skip
            res.tagged += 1
            subject, why = self.subject(tags)
            if subject is None:
                with self.db.tx():
                    self.db.update(rec["hash"], status="review", reason=why)
                    self.report_item(rec["hash"])
                res.review += 1
                continue
            self.file(rec["hash"], subject)
            res.filed += 1
        return dirty

    def subject(self, tags: list[str]) -> tuple[str | None, str]:
        """The one subject folder whose tags best match, or (None, why) for review."""
        if not tags:
            return None, "tags could not be inferred: the model returned no tags"
        have = {t.casefold() for t in tags}
        scores = [(len(have & set(words)), folder) for folder, words in self.box.subjects]
        best = max((s for s, _ in scores), default=0)
        if best == 0:
            return None, "no subject folder matches the tags"
        top = [f for s, f in scores if s == best]
        if len(top) > 1:
            return None, f"tags match more than one subject folder: {', '.join(top)}"
        return top[0], ""

    def _dest_rel(self, rec: dict[str, Any], subject: str) -> str:
        """``<subject>/<date>/<name>``, else a free or same-content variant: never another
        file's name."""
        parts = rec["rel"].split("/")
        date = next((p for p in parts[2:-1] if DATE.fullmatch(p)), rec["arrived_at"][:10])
        p = Path(parts[-1])
        names = [p.name, f"{p.stem}-{rec['hash'][:8]}{p.suffix}"]
        names += [f"{p.stem}-{rec['hash'][:8]}-{n}{p.suffix}" for n in range(2, 100)]
        for name in names:
            rel = f"{subject}/{date}/{name}"
            dest = self._abs(rel)
            if not dest.exists() or sha256(dest) == rec["hash"]:
                return rel
        raise InboxError(f"no free name for {rec['rel']} in {subject}/{date}")

    def file(self, h: str, subject: str) -> None:
        rec = self.db.item(h) or {}
        dest_rel = self._dest_rel(rec, subject)
        self.db.update(h, status="filing", dest=dest_rel)  # the intent, before any byte moves
        self._move(h)

    def _move(self, h: str) -> None:
        """Finish one in-flight move; safe to repeat after a kill at any step."""
        rec = self.db.item(h) or {}
        src, dest = self._abs(rec["rel"]), self._abs(rec["dest"])
        if src.exists():
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(src, dest)  # same share: atomic
                except OSError:
                    tmp = dest.with_name(f".{dest.name}.{h[:8]}.part")
                    shutil.copy2(src, tmp)
                    if sha256(tmp) != h:
                        tmp.unlink(missing_ok=True)
                        raise InboxError(f"copy of {rec['rel']} did not verify") from None
                    os.replace(tmp, dest)
            if src.exists():  # dest was already there: unlink only over verified bytes
                if sha256(dest) != h:
                    self.db.update(h, status="waiting", dest=None)
                    raise InboxError(f"{rec['dest']} is taken by other media; not overwritten")
                src.unlink()
        elif not dest.exists() or sha256(dest) != h:
            self.db.update(h, status="missing")
            return
        with self.db.tx():
            self.db.q("DELETE FROM seen WHERE rel=?", rec["rel"])
            self.db.update(h, status="filed", rel=rec["dest"], reason=None)
            self.report_item(h)

    def reconcile(self) -> bool:
        rows = self.db.q("SELECT hash FROM items WHERE status='filing'")
        for row in rows:
            self._move(row["hash"])
        return bool(rows)

    # ---- catalog ---------------------------------------------------------------------------
    def _index_path(self) -> Path:
        return self._abs(f"{CATALOG_DIR}/{INDEX_NAME}")

    def record(self, rec: dict[str, Any]) -> dict[str, Any]:
        """The catalog record: root-relative, size + mtime, never an absolute path."""
        out: dict[str, Any] = {"root": self.root, "relpath": rec["rel"], "media": rec["media"]}
        if rec["tags"] is not None:
            out.update(caption=rec["caption"], tags=json.loads(rec["tags"]))
        else:
            out["failed"] = rec["reason"] or "tags could not be inferred"
        stamp = json.loads(rec["stamp"]) if rec["stamp"] else {}
        with contextlib.suppress(OSError, InboxError, JobError):
            stamp = file_stamp(self._abs(rec["rel"]))
        out.update(stamp)
        out.update(sha256=rec["hash"], feeder=rec["feeder"], arrived_at=rec["arrived_at"])
        if rec["status"] == "review":
            out["review"] = rec["reason"]
        if rec["dupes"] != "[]":
            out["duplicates"] = json.loads(rec["dupes"])
        return out

    def write_catalog(self) -> str:
        rows = self.db.q(
            "SELECT * FROM items WHERE status IN ('filed', 'review') ORDER BY arrived_at, hash"
        )
        records = [self.record(dict(r)) for r in rows]
        index = self._index_path()
        write_records(index, {}, records)
        tagged = sum(1 for r in records if "tags" in r)
        note = update_catalog(
            self.roots,
            lambda ref: self.ctx.allowed(ref, what="catalog folder"),
            index_path=index,
            folder_path=self._abs(self.box.inbox),
            counts={
                "files": len(records),
                "tagged": tagged,
                "skipped": 0,
                "failed": len(records) - tagged,
            },  # fmt: skip
            model=self.box.model,
        )
        log.info("inbox catalog: %s", note)
        return note

    # ---- reporting -------------------------------------------------------------------------
    def report_item(self, h: str) -> None:
        rec = self.db.item(h)
        if rec is None:
            return
        body: dict[str, Any] = {
            "feeder_id": rec["feeder"],
            "root": self.root,
            "path": rec["rel"],
            "kind": "video" if rec["media"] == "video" else "photo",
            "bytes": rec["bytes"],
            "arrived_at": rec["arrived_at"],
            "tag_status": {"filed": "filed"}.get(rec["status"], "tagged" if rec["tags"]
                                                  else "waiting"),
        }  # fmt: skip
        if rec["tags"]:
            body["tags"] = [t[:60] for t in json.loads(rec["tags"]) if t][:50]
        if rec["status"] == "filed":
            body["filed_to"] = rec["rel"]
        self.db.put(f"item:{h}", "media_item", h[:64], body)

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {
            fid: {"photos_waiting": 0, "videos_waiting": 0, "bytes_waiting": 0}
            for fid in self.feeders
        }
        rows = self.db.q(
            "SELECT feeder, media, COUNT(*) n, COALESCE(SUM(bytes), 0) b FROM items"
            " WHERE status IN ('waiting', 'review') GROUP BY feeder, media"
        )
        for r in rows:
            c = out.setdefault(
                r["feeder"], {"photos_waiting": 0, "videos_waiting": 0, "bytes_waiting": 0}
            )
            c["videos_waiting" if r["media"] == "video" else "photos_waiting"] += r["n"]
            c["bytes_waiting"] += r["b"]
        return out

    def report(self, started: str, res: PassResult) -> None:
        per = self.counts()
        sources = [{"feeder_id": fid, **c} for fid, c in sorted(per.items())][:100]
        totals = {k: sum(s[k] for s in sources) for k in
                  ("photos_waiting", "videos_waiting", "bytes_waiting")}  # fmt: skip
        last_run = {"status": res.status, "started_at": started, "finished_at": utc_iso()}
        if res.error:
            last_run["error"] = res.error[:500]
        as_of = datetime.now(UTC).isoformat(timespec="microseconds")
        body = {"watcher": self.box.watcher, "host": self.cfg.host_name[:100], "as_of": as_of,
                "sources": sources, "totals": totals, "last_run": last_run}  # fmt: skip
        self.db.put("heartbeat", "media_heartbeat", self.box.watcher, body)
        self.last_counts = totals
        for f in self.feeders.values():
            if f.kind == "native":
                self._infer_pull(f.feeder_id)

    def _infer_pull(self, fid: str) -> None:
        """A native sync's last pull, inferred from its folder: the newest arrival's mtime."""
        row = self.db.q("SELECT MAX(src_mtime) m, COUNT(*) n FROM items WHERE feeder=?", fid)[0]
        if row["m"] is None:
            return
        since = (datetime.now(UTC) - timedelta(hours=24)).isoformat(timespec="seconds")
        day = self.db.q(
            "SELECT COUNT(*) n FROM items WHERE feeder=? AND arrived_at >= ?", fid, since
        )[0]["n"]
        report = {"ok": True, "pulled_at": utc_iso(row["m"]), "items_24h": day,
                  "items_total": row["n"]}  # fmt: skip
        key = json.dumps(report, sort_keys=True)
        if self.db.meta(f"pull:{fid}") != key:
            self.db.set_meta(f"pull:{fid}", key)
            self.db.put(f"pull:{fid}", "media_pull", fid, report)

    def flush(self) -> int:
        """Replay the outbox oldest first; stop (keeping the rest) when the backbone is down."""
        sent = 0
        for row in self.db.queued():
            if self.stop.is_set():
                break
            op, args = row["op"], json.loads(row["args"])
            try:
                if op == "media_sync_finish":
                    self.bb.media_sync_finish(args[0], **args[1])
                else:
                    getattr(self.bb, op)(*args)
            except BackboneUnavailable as exc:
                log.info("inbox outbox: backbone unreachable (%s); %s queued", exc,
                         len(self.db.queued()))  # fmt: skip
                break
            except BackboneError as exc:
                if exc.status in (401, 403):
                    log.warning("inbox outbox: refused (%s); kept for replay: %s", exc.status,
                                exc.detail)  # fmt: skip
                    break
                if op == "media_pull" and exc.status == 404 and row["tries"] < PULL_404_TRIES:
                    self.db.q("UPDATE outbox SET tries=tries+1 WHERE seq=?", row["seq"])
                    continue
                log.warning("inbox outbox: %s %s refused: %s", op, args[0], exc)
            self.db.done(row["seq"])
            sent += 1
        return sent

    # ---- sync commands ---------------------------------------------------------------------
    def handle_syncs(self) -> int:
        self.flush()  # a finished command still queued is delivered, never run twice
        try:
            mine = [c for c in self.bb.media_syncs("acked")
                    if (c.get("data") or {}).get("acked_by") == self.cfg.actor]  # fmt: skip
            pending = self.bb.media_syncs("pending")
        except (BackboneUnavailable, BackboneError) as exc:
            log.debug("inbox: sync poll failed: %s", exc)
            return 0
        handled = 0
        for cmd in [*mine, *pending]:
            data, cid = cmd.get("data") or {}, str(cmd.get("id"))
            feeder = self.feeders.get(str(data.get("feeder_id")))
            if feeder is None or self.stop.is_set():
                continue  # another worker's feeder: not ours to ack
            if self.db.has(f"finish:{cid}"):
                continue  # ran already; its outcome waits in the outbox
            if cmd.get("status", "pending") == "pending":
                try:
                    self.bb.media_sync_ack(cid)
                except BackboneUnavailable:
                    return handled
                except BackboneError as exc:
                    log.info("inbox: sync %s not acked: %s", cid, exc)
                    continue
            ok, items, error = self.sync(feeder)
            self.db.put(f"finish:{cid}", "media_sync_finish", cid,
                        {"ok": ok, "items": items, "error": error})  # fmt: skip
            handled += 1
        self.flush()
        return handled

    def sync(self, feeder: Feeder) -> tuple[bool, int | None, str | None]:
        """(ok, items, error) for one sync command. An API feeder pulls, then re-scans; a native
        sync re-scans now (the NAS syncs it on its own schedule: no pull is claimed)."""
        pulled: int | None = None
        if feeder.kind == "api":
            puller = PULLERS.get(feeder.feeder_id)
            if puller is None:
                return False, None, f"no puller for api feeder {feeder.feeder_id} on this runner"
            try:
                pulled = int(puller(self._abs(f"{self.box.inbox}/{feeder.folder}")))
            except Exception as exc:  # a puller is plug-in code: its failure is the outcome
                report = {"ok": False, "pulled_at": utc_iso(), "error": str(exc)[:500]}
                self.db.put(f"pull:{feeder.feeder_id}", "media_pull", feeder.feeder_id, report)
                return False, None, f"pull failed: {exc}"
            report = {"ok": True, "pulled_at": utc_iso()}
            self.db.put(f"pull:{feeder.feeder_id}", "media_pull", feeder.feeder_id, report)
        res = self.run_pass(f"sync {feeder.feeder_id}")
        items = pulled if pulled is not None else res.new[feeder.feeder_id]
        if res.status != "ok":
            return False, items, f"re-scan failed: {res.error}"
        return True, items, None

    # ---- the loop --------------------------------------------------------------------------
    def run_forever(self, poll_seconds: float = 60.0) -> None:
        next_pass = 0.0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_pass or self.rescan.is_set():
                    self.rescan.clear()
                    self.run_pass()
                    next_pass = time.monotonic() + self.box.interval_seconds
                self.handle_syncs()
            except Exception:  # the watcher never takes the runner down with it
                log.exception("inbox watcher pass crashed; next pass on the cadence")
                next_pass = time.monotonic() + self.box.interval_seconds
            wait = max(1.0, min(poll_seconds, next_pass - time.monotonic()))
            self.stop.wait(wait)
