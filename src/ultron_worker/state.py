"""The runner's local state: job checkpoints in SQLite plus the live status board.

SQLite is the checkpoint: every finished work item is committed with its result before the
next one starts, so a job killed by sleep, reboot or a restart resumes at the first item with
no committed result. Output files are always rebuilt from these rows (atomic replace), so a
resume can never duplicate an output line.

The :class:`StatusBoard` is the one state behind both the backbone progress events and the
localhost GUI: the runner writes it, the GUI only reads :meth:`StatusBoard.snapshot`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    task_id TEXT PRIMARY KEY,
    title TEXT, kind TEXT, model TEXT, output TEXT,
    status TEXT NOT NULL,  -- running|retry_wait|finished|failing|done|needs_david
    attempts INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
    current_item TEXT,
    active_seconds REAL NOT NULL DEFAULT 0,
    started_at TEXT, finished_at TEXT, updated_at TEXT,
    error TEXT, outputs TEXT, summary TEXT,
    spec TEXT,  -- kind/model/params the checkpoints were made with
    report_step INTEGER NOT NULL DEFAULT 0  -- report calls already delivered
);
CREATE TABLE IF NOT EXISTS items (
    task_id TEXT NOT NULL, key TEXT NOT NULL, result TEXT NOT NULL, done_at TEXT NOT NULL,
    timing TEXT,  -- {phase: seconds} of the run that produced this result
    PRIMARY KEY (task_id, key)
);
"""

# Columns added after the first schema: a state DB from an older runner gets them on open.
MIGRATIONS = (
    ("jobs", "spec", "TEXT"),
    ("jobs", "report_step", "INTEGER NOT NULL DEFAULT 0"),
    ("items", "timing", "TEXT"),
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(SCHEMA)
            for table, column, decl in MIGRATIONS:
                have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- jobs ---------------------------------------------------------------
    def job(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
        return _job(row) if row else None

    def start_job(
        self,
        task_id: str,
        *,
        title: str,
        kind: str,
        model: str,
        output: str,
        spec: str,
        fresh_dispatch: bool,
    ) -> dict[str, Any]:
        """Open (or reopen) a job row. A fresh dispatch (David moved the card to dispatched,
        first time or after needs_david) resets the attempt count; the item checkpoints stay,
        so a re-dispatched job resumes where it stopped - unless its kind, model or params
        changed, which makes the old results incompatible: then it starts over."""
        ts = now_iso()
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO jobs (task_id,title,kind,model,output,spec,status,started_at,"
                    "updated_at) VALUES (?,?,?,?,?,?,'running',?,?)",
                    (task_id, title, kind, model, output, spec, ts, ts),
                )
            else:
                attempts = 0 if fresh_dispatch else row["attempts"]
                if row["spec"] != spec:
                    db.execute("DELETE FROM items WHERE task_id=?", (task_id,))
                    db.execute(
                        "UPDATE jobs SET done=0,total=0,active_seconds=0,started_at=? "
                        "WHERE task_id=?",
                        (ts, task_id),
                    )
                db.execute(
                    "UPDATE jobs SET title=?,kind=?,model=?,output=?,spec=?,status='running',"
                    "attempts=?,error=NULL,finished_at=NULL,report_step=0,updated_at=? "
                    "WHERE task_id=?",
                    (title, kind, model, output, spec, attempts, ts, task_id),
                )
        job = self.job(task_id)
        assert job is not None
        return job

    def update_job(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "outputs" in fields:
            fields["outputs"] = json.dumps(fields["outputs"])
        fields["updated_at"] = now_iso()
        cols = ",".join(f"{k}=?" for k in fields)
        with self._tx() as db:
            db.execute(f"UPDATE jobs SET {cols} WHERE task_id=?", (*fields.values(), task_id))

    def add_active(self, task_id: str, seconds: float) -> float:
        with self._tx() as db:
            db.execute(
                "UPDATE jobs SET active_seconds=MAX(0, active_seconds+?) WHERE task_id=?",
                (seconds, task_id),
            )
            row = db.execute(
                "SELECT active_seconds FROM jobs WHERE task_id=?", (task_id,)
            ).fetchone()
        return float(row["active_seconds"]) if row else 0.0

    def recent(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status IN ('done','finished') "
                "ORDER BY finished_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_job(r) for r in rows]

    def failures(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE status IN ('needs_david','failing','retry_wait') "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_job(r) for r in rows]

    # ---- item checkpoints ---------------------------------------------------
    def item_results(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key, result FROM items WHERE task_id=?", (task_id,)
            ).fetchall()
        return {r["key"]: json.loads(r["result"]) for r in rows}

    def item_timing(self, task_id: str, key: str) -> dict[str, float] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT timing FROM items WHERE task_id=? AND key=?", (task_id, key)
            ).fetchone()
        return json.loads(row["timing"]) if row and row["timing"] else None

    def checkpoint(
        self,
        task_id: str,
        key: str,
        result: Any,
        *,
        done: int,
        current_item: str | None,
        timing: dict[str, float] | None = None,
    ) -> None:
        """Commit one item's result and the job's progress in a single transaction."""
        ts = now_iso()
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO items (task_id,key,result,done_at,timing) "
                "VALUES (?,?,?,?,?)",
                (task_id, key, json.dumps(result), ts, json.dumps(timing) if timing else None),
            )
            db.execute(
                "UPDATE jobs SET done=?, current_item=?, updated_at=? WHERE task_id=?",
                (done, current_item, ts, task_id),
            )


def _job(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["outputs"] = json.loads(d["outputs"]) if d.get("outputs") else []
    return d


class StatusBoard:
    """Live runner state shared by the runner threads and the GUI (read-only for the GUI)."""

    def __init__(
        self, store: Store, *, actor: str, host: str, version: str, poll_seconds: float
    ) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._s: dict[str, Any] = {
            "actor": actor,
            "host": host,
            "version": version,
            "poll_seconds": poll_seconds,
            "started_at": now_iso(),
            "connection": "connecting",
            "connection_detail": None,
            "last_poll_at": None,
            "last_heartbeat_at": None,
            "last_contact_at": None,
            "slept": None,
            "paused_by_sleep": False,
        }
        self._current: dict[str, Any] | None = None
        self._queue: list[dict[str, Any]] = []
        self.sleep_seconds_total = 0.0  # cumulative detected suspend time (Clock reads it)

    # ---- writers (runner) ---------------------------------------------------
    def set(self, **fields: Any) -> None:
        with self._lock:
            self._s.update(fields)

    def contact(self, ok: bool, detail: str | None = None) -> None:
        with self._lock:
            if ok:
                self._s.update(
                    connection="online",
                    connection_detail=None,
                    last_contact_at=now_iso(),
                    paused_by_sleep=False,
                )
            else:
                self._s.update(connection="offline", connection_detail=detail)

    def slept(self, seconds: float) -> None:
        with self._lock:
            self.sleep_seconds_total += seconds
            self._s.update(
                slept={"seconds": round(seconds), "woke_at": now_iso()}, paused_by_sleep=True
            )

    def set_queue(self, queue: list[dict[str, Any]]) -> None:
        with self._lock:
            self._queue = queue

    def set_current(self, current: dict[str, Any] | None) -> None:
        with self._lock:
            self._current = dict(current) if current else None

    def update_current(self, **fields: Any) -> None:
        with self._lock:
            if self._current is not None:
                self._current.update(fields)

    # ---- readers ------------------------------------------------------------
    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._current) if self._current else None

    def activity(self) -> str:
        with self._lock:
            if self._s["paused_by_sleep"]:
                return "paused-by-sleep"
            return "working" if self._current else "idle"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            runner = dict(self._s)
            current = dict(self._current) if self._current else None
            queue = [dict(q) for q in self._queue]
        runner["activity"] = (
            "paused-by-sleep" if runner.pop("paused_by_sleep") else "working" if current else "idle"
        )
        if current and current.get("total"):
            current["percent"] = round(100.0 * current.get("done", 0) / current["total"], 1)
        elif current:
            current["percent"] = 0.0
        recent = [
            {
                "task_id": j["task_id"],
                "title": j["title"],
                "kind": j["kind"],
                "model": j["model"],
                "finished_at": j["finished_at"],
                "duration_s": round(j["active_seconds"]),
                "outputs": j["outputs"],
                "summary": j["summary"],
                "items": j["total"],
                "reported": j["status"] == "done",
            }
            for j in self.store.recent()
        ]
        attention = [
            {
                "task_id": j["task_id"],
                "title": j["title"],
                "kind": j["kind"],
                "status": j["status"],
                "error": j["error"],
                "attempts": j["attempts"],
                "at": j["updated_at"],
            }
            for j in self.store.failures()
        ]
        return {
            "runner": runner,
            "current": current,
            "queue": queue,
            "recent": recent,
            "attention": attention,
            "generated_at": now_iso(),
            "ts": time.time(),
        }
