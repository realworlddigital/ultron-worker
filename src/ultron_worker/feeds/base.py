"""Shared machinery of the API feeders: read-only HTTP, the pull state and landing a file.

A feeder lands files only in its own inbox folder, ``<inbox>/<folder>/<YYYY-MM-DD>/``, each with
one ``manifest.jsonl`` line; hashing, tagging, filing and the catalog are the watcher's.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MANIFEST = "manifest.jsonl"
CHUNK = 1 << 20
UNSAFE = re.compile(r"[^A-Za-z0-9._ ()-]+")
MAX_NAME = 120


class FeedError(RuntimeError):
    """A pull failed: the registry shows status error with this message."""


class NotConfigured(FeedError):
    """David's part (identity or credential) is missing: no network call is made."""

    def __init__(self, what: str) -> None:
        super().__init__(f"not configured: {what}")


class AuthRejected(FeedError):
    """The stored credential was refused: the feeder stays stopped until it changes."""


class FeedUnavailable(FeedError):
    """Network down or the service failing: the next cadence tries again."""


class HttpError(FeedError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


class _SameHostAuth(urllib.request.HTTPRedirectHandler):
    """Redirects stay on https, and a credential header never follows one to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        if urllib.parse.urlparse(newurl).scheme != "https":
            raise FeedError(f"refusing a redirect off https: {newurl[:100]}")
        old_host = urllib.parse.urlparse(req.full_url).hostname
        if urllib.parse.urlparse(newurl).hostname != old_host:
            for name in ("Authorization", "authorization"):
                new.headers.pop(name, None)
                new.unredirected_hdrs.pop(name, None)
        return new


class ReadOnlyHttp:
    """The feeders' whole network surface: GET from the two sources' hosts, plus the one OAuth
    token refresh POST. Nothing here can post, delete or modify anything in Discord or Drive."""

    GET_HOSTS = frozenset(
        {"discord.com", "cdn.discordapp.com", "media.discordapp.net", "www.googleapis.com"}
    )
    TOKEN_HOSTS = frozenset({"oauth2.googleapis.com"})
    TIMEOUT = 60.0

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_SameHostAuth())

    @staticmethod
    def _allowed(url: str, hosts: frozenset[str]) -> None:
        u = urllib.parse.urlparse(url)
        if u.scheme != "https" or u.hostname not in hosts:
            raise FeedError(f"refusing {u.scheme}://{u.hostname}: not a feeder source host")

    @contextlib.contextmanager
    def _open(self, req: urllib.request.Request) -> Iterator[Any]:
        try:
            with self._opener.open(req, timeout=self.TIMEOUT) as resp:
                yield resp
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:500]
            if exc.code >= 500 or exc.code == 429:
                raise FeedUnavailable(f"HTTP {exc.code} from {req.host}: {body[:200]}") from exc
            raise HttpError(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            raise FeedUnavailable(f"{req.host}: {getattr(exc, 'reason', exc)}") from exc

    def get_json(self, url: str, headers: dict[str, str]) -> Any:
        self._allowed(url, self.GET_HOSTS)
        req = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
        with self._open(req) as resp:
            return json.loads(resp.read() or b"null")

    def download(self, url: str, headers: dict[str, str], dest: Path, stop: threading.Event) -> int:
        self._allowed(url, self.GET_HOSTS)
        size = 0
        with (
            self._open(urllib.request.Request(url, headers=headers)) as resp,
            open(dest, "wb") as out,
        ):
            while chunk := resp.read(CHUNK):
                if stop.is_set():
                    raise FeedUnavailable("stopping")
                out.write(chunk)
                size += len(chunk)
        return size

    def token(self, url: str, form: dict[str, str]) -> dict[str, Any]:
        self._allowed(url, self.TOKEN_HOSTS)
        data = urllib.parse.urlencode(form).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )  # fmt: skip
        with self._open(req) as resp:
            return dict(json.loads(resp.read() or b"{}"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor (
    feeder TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, PRIMARY KEY (feeder, key)
);
CREATE TABLE IF NOT EXISTS pulled (
    feeder TEXT NOT NULL, source_id TEXT NOT NULL, version TEXT NOT NULL, rel TEXT NOT NULL,
    bytes INTEGER NOT NULL, pulled_at TEXT NOT NULL, PRIMARY KEY (feeder, source_id, version)
);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


class FeedState:
    """``<state_dir>/feeds.sqlite3``: per-feeder cursors (Discord's last message id, Drive's
    modified-time watermark) and every file landed, so nothing known is downloaded twice."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def get(self, feeder: str, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM cursor WHERE feeder=? AND key=?", (feeder, key)
            ).fetchone()
        return row[0] if row else None

    def set(self, feeder: str, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO cursor(feeder, key, value) VALUES (?, ?, ?)",
                (feeder, key, value),
            )

    def known(self, feeder: str, source_id: str, version: str) -> bool:
        with self._lock:
            return bool(
                self._db.execute(
                    "SELECT 1 FROM pulled WHERE feeder=? AND source_id=? AND version=?",
                    (feeder, source_id, version),
                ).fetchone()
            )

    def landed(
        self, feeder: str, source_id: str, version: str, rel: str, size: int,
        cursor: dict[str, str] | None = None,
    ) -> None:  # fmt: skip
        """One file landed, and the cursor that moves past it, in one transaction."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(
                    "INSERT OR REPLACE INTO pulled VALUES (?, ?, ?, ?, ?, ?)",
                    (feeder, source_id, version, rel, size, utc_now().isoformat()),
                )
                for k, v in (cursor or {}).items():
                    self._db.execute(
                        "INSERT OR REPLACE INTO cursor(feeder, key, value) VALUES (?, ?, ?)",
                        (feeder, k, v),
                    )
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def counts(self, feeder: str) -> tuple[int, int]:
        """(items landed in the last 24 h, items landed ever)."""
        since = (utc_now() - timedelta(hours=24)).isoformat()
        with self._lock:
            day = self._db.execute(
                "SELECT COUNT(*) FROM pulled WHERE feeder=? AND pulled_at >= ?", (feeder, since)
            ).fetchone()[0]
            total = self._db.execute(
                "SELECT COUNT(*) FROM pulled WHERE feeder=?", (feeder,)
            ).fetchone()[0]
        return int(day), int(total)


def safe_name(name: str) -> str:
    """A source's file name as a plain, Windows-safe file name (no path parts)."""
    base = re.split(r"[\\/]", name)[-1]
    base = UNSAFE.sub("_", base).strip(" .") or "file"
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    ext = ext[:10]
    keep = MAX_NAME - len(ext) - 1
    return f"{stem[:keep]}.{ext}" if ext else base[:MAX_NAME]


def _free(day_dir: Path, name: str, source_id: str) -> Path:
    target = day_dir / name
    if not target.exists():
        return target
    stem, ext = os.path.splitext(name)
    tag = re.sub(r"[^A-Za-z0-9-]", "", source_id)[:16] or "x"
    n = 0
    while True:
        target = day_dir / f"{stem}-{tag}{f'-{n}' if n else ''}{ext}"
        if not target.exists():
            return target
        n += 1


def land(
    source_dir: Path,
    day: str,
    name: str,
    source_id: str,
    fetch: Callable[[Path], int],
    mtime: float | None,
    manifest: dict[str, Any],
) -> tuple[Path, int]:
    """Download into ``<source_dir>/<day>/`` atomically (a dot-prefixed ``.part`` the watcher
    never reads, then a rename), never over an existing file, then append the manifest line.
    The file keeps the source's timestamp as its mtime."""
    day_dir = source_dir / day
    day_dir.mkdir(parents=True, exist_ok=True)
    target = _free(day_dir, safe_name(name), source_id)
    tmp = day_dir / f".{target.name}.part"
    try:
        size = fetch(tmp)
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    line = {"file": target.name, **manifest, "bytes": size, "pulled_at": utc_now().isoformat()}
    with open(day_dir / MANIFEST, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    return target, size


def iso_ts(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def today() -> str:
    """The inbox day folder: the worker host's local date of the pull (arrival day)."""
    return datetime.now().strftime("%Y-%m-%d")
