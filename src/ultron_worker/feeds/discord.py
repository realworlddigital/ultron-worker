"""The Discord thread feeder: image attachments from ONE Guild Hall thread, read-only.

The bot token is David's (``ULTRON_DISCORD_TOKEN`` or ``media_feeders.discord.token_file``),
read at pull time and never prompted for, generated or stored. The only calls are GETs: the
thread (to confirm it sits in the configured server), its messages after the last one pulled,
and the attachments' CDN URLs (fetched without the token).
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .. import __version__
from ..config import DiscordFeedConfig
from .base import (
    AuthRejected,
    FeedError,
    FeedState,
    HttpError,
    ReadOnlyHttp,
    iso_ts,
    land,
    today,
)

API = "https://discord.com/api/v10"
TOKEN_ENV = "ULTRON_DISCORD_TOKEN"
THREAD_TYPES = frozenset({10, 11, 12})  # announcement, public and private threads
PAGE = 100
IMAGE_EXTS = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff"}
)
UA = f"DiscordBot (https://github.com/realworlddigital/ultron-worker, {__version__})"


def is_image(att: Mapping[str, Any]) -> bool:
    ctype = str(att.get("content_type") or "").lower()
    if ctype:
        return ctype.startswith("image/")
    return os.path.splitext(str(att.get("filename") or ""))[1].lower() in IMAGE_EXTS


class DiscordFeed:
    def __init__(
        self,
        cfg: DiscordFeedConfig,
        http: ReadOnlyHttp,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.env = os.environ if env is None else env
        self.feeder_id = cfg.feeder_id
        self.interval_seconds = cfg.interval_seconds
        self._rejected: str | None = None  # fingerprint of a token Discord refused (memory only)

    def _token(self) -> str:
        token = str(self.env.get(TOKEN_ENV) or "").strip()
        if not token and self.cfg.token_file:
            try:
                token = Path(self.cfg.token_file).read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        return token

    @staticmethod
    def _fingerprint(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()[:16]

    def problem(self) -> str | None:
        """Why a pull cannot start, checked locally (no network): None when it can."""
        missing = [k for k in ("guild_id", "thread_id") if not getattr(self.cfg, k)]
        if missing:
            keys = " and ".join(f"media_feeders.discord.{k}" for k in missing)
            return f"not configured: {keys} not set"
        token = self._token()
        if not token:
            return f"not configured: no bot token ({TOKEN_ENV} or media_feeders.discord.token_file)"
        if self._rejected == self._fingerprint(token):
            return "bot token rejected by Discord (401): stopped until the token changes"
        return None

    def _get(self, url: str, token: str) -> Any:
        try:
            return self.http.get_json(url, {"Authorization": f"Bot {token}", "User-Agent": UA})
        except HttpError as exc:
            tid = self.cfg.thread_id
            if exc.status == 401:
                self._rejected = self._fingerprint(token)
                raise AuthRejected(
                    "bot token rejected by Discord (401): stopped until the token changes"
                ) from exc
            if exc.status == 403:
                raise FeedError(f"the bot has no read access to thread {tid} (403)") from exc
            if exc.status == 404:
                raise FeedError(f"thread {tid} not found (404): check the thread id") from exc
            raise FeedError(f"Discord HTTP {exc.status}: {exc.body[:200]}") from exc

    def pull(self, source_dir: Path, state: FeedState, stop: threading.Event) -> int:
        token = self._token()
        guild, tid = self.cfg.guild_id, self.cfg.thread_id
        thread = self._get(f"{API}/channels/{tid}", token)
        if str(thread.get("guild_id")) != guild or thread.get("type") not in THREAD_TYPES:
            raise FeedError(f"channel {tid} is not a thread in server {guild}")
        key = f"after:{tid}"
        after = state.get(self.feeder_id, key) or "0"
        landed = 0
        while not stop.is_set():
            batch = self._get(f"{API}/channels/{tid}/messages?after={after}&limit={PAGE}", token)
            if not isinstance(batch, list):
                raise FeedError("Discord returned an unexpected message page")
            for msg in sorted(batch, key=lambda m: int(m["id"])):
                if stop.is_set():
                    return landed
                if int(msg["id"]) <= int(after):
                    continue  # a known message id: never fetched again
                landed += self._message(msg, source_dir, state, stop)
                after = str(msg["id"])
                state.set(self.feeder_id, key, after)
            if len(batch) < PAGE:
                break
        return landed

    def _message(
        self, msg: Mapping[str, Any], source_dir: Path, state: FeedState, stop: threading.Event
    ) -> int:
        author = msg.get("author") or {}
        n = 0
        for att in msg.get("attachments") or []:
            aid = str(att.get("id") or "")
            url = str(att.get("url") or "")
            if not aid or not url or not is_image(att):
                continue
            if state.known(self.feeder_id, aid, "1"):
                continue  # landed before the cursor moved (a pull cut short mid-message)
            manifest = {
                "source": "discord",
                "source_id": aid,
                "message_id": str(msg["id"]),
                "guild_id": self.cfg.guild_id,
                "thread_id": self.cfg.thread_id,
                "author": {
                    "id": str(author.get("id") or ""),
                    "username": author.get("username"),
                    "display_name": author.get("global_name"),
                },
                "timestamp": msg.get("timestamp"),
                "filename": att.get("filename"),
                "content_type": att.get("content_type"),
            }
            target, size = land(
                source_dir,
                today(),
                str(att.get("filename") or f"{aid}.png"),
                aid,
                lambda tmp, u=url: self.http.download(u, {"User-Agent": UA}, tmp, stop),
                iso_ts(str(msg.get("timestamp") or "")),
                manifest,
            )
            state.landed(self.feeder_id, aid, "1", f"{target.parent.name}/{target.name}", size)
            n += 1
        return n
