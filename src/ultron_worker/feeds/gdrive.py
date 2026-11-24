"""The Google Drive feeder: new/changed images and videos from ONE Drive folder, read-only.

Auth is David's stored Google sign-in (``media_feeders.gdrive.auth_file``: the authorized-user
JSON his standard Google sign-in writes, scope ``drive.readonly``). The feeder only reads it and
refreshes a short-lived access token in memory; if the sign-in is missing, expired or revoked it
reports the error and stops. Incremental by a modified-time watermark kept per folder.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from ..config import DriveFeedConfig
from .base import (
    AuthRejected,
    FeedError,
    FeedState,
    HttpError,
    NotConfigured,
    ReadOnlyHttp,
    iso_ts,
    land,
    media_name,
    today,
)

FILES = "https://www.googleapis.com/drive/v3/files"
TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FIELDS = (
    "nextPageToken,files(id,name,mimeType,modifiedTime,size,md5Checksum,capabilities/canDownload)"
)
AUTH_KEYS = ("client_id", "client_secret", "refresh_token")
log = logging.getLogger(__name__)
EXPIRED = "Google sign-in expired or revoked: stopped until David signs in again"


class _CannotDownload(FeedError):
    """This one file is listed but the signed-in account may not download it: skip it."""


class DriveFeed:
    def __init__(
        self, cfg: DriveFeedConfig, http: ReadOnlyHttp, clock: Callable[[], float] = time.time
    ) -> None:
        self.cfg = cfg
        self.http = http
        self.clock = clock
        self.feeder_id = cfg.feeder_id
        self.interval_seconds = cfg.interval_seconds
        self._access: tuple[str, float] | None = None
        self._dead: float | None = None  # mtime of an auth file Google refused

    def _auth_path(self) -> Path:
        return Path(self.cfg.auth_file)

    def _auth(self) -> dict[str, Any]:
        path = self._auth_path()
        try:
            auth = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise NotConfigured(f"no Google sign-in at {path}") from None
        except (OSError, ValueError) as exc:
            raise NotConfigured(f"{path} is not readable sign-in JSON ({exc})") from None
        if not isinstance(auth, dict) or not all(auth.get(k) for k in AUTH_KEYS):
            raise NotConfigured(f"{path} is not a Google authorized-user sign-in")
        return auth

    def problem(self) -> str | None:
        """Why a pull cannot start, checked locally (no network): None when it can."""
        if not self.cfg.folder_id:
            return "not configured: media_feeders.gdrive.folder_id not set"
        if not self.cfg.auth_file:
            return "not configured: media_feeders.gdrive.auth_file not set"
        try:
            self._auth()
            mtime = self._auth_path().stat().st_mtime
        except NotConfigured as exc:
            return str(exc)
        except OSError as exc:
            return f"not configured: {exc}"
        if self._dead is not None and self._dead == mtime:
            return EXPIRED
        return None

    def _token(self) -> str:
        if self._access and self.clock() < self._access[1] - 60:
            return self._access[0]
        auth = self._auth()
        try:
            got = self.http.token(
                str(auth.get("token_uri") or TOKEN_URI),
                {
                    "client_id": auth["client_id"],
                    "client_secret": auth["client_secret"],
                    "refresh_token": auth["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
        except HttpError as exc:
            if exc.status in (400, 401):  # invalid_grant: expired / revoked refresh token
                self._dead = self._auth_path().stat().st_mtime
                raise AuthRejected(f"{EXPIRED} ({exc.status})") from exc
            raise FeedError(f"Google token refresh HTTP {exc.status}") from exc
        if not got.get("access_token"):
            raise FeedError("Google token refresh returned no access token")
        self._access = (str(got["access_token"]), self.clock() + float(got.get("expires_in", 3600)))
        return self._access[0]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _call(self, fn: Callable[[dict[str, str]], Any], download: bool = False) -> Any:
        """One Drive call; a 401 drops the access token and retries once with a fresh one."""
        for attempt in (0, 1):
            try:
                return fn(self._headers())
            except HttpError as exc:
                if exc.status == 401 and attempt == 0:
                    self._access = None
                    continue
                if exc.status == 401:
                    raise AuthRejected(f"{EXPIRED} (401)") from exc
                if download and (exc.status == 404 or "cannotDownload" in exc.body):
                    raise _CannotDownload(f"HTTP {exc.status}: {exc.body[:120]}") from exc
                if exc.status in (403, 404):
                    raise FeedError(
                        f"Drive folder {self.cfg.folder_id} not found or not shared with the "
                        f"signed-in account ({exc.status})"
                    ) from exc
                raise FeedError(f"Drive HTTP {exc.status}: {exc.body[:200]}") from exc
        raise AssertionError("unreachable")

    def query(self, watermark: str | None) -> str:
        q = (
            f"'{self.cfg.folder_id}' in parents and trashed = false and "
            "(mimeType contains 'image/' or mimeType contains 'video/')"
        )
        return f"{q} and modifiedTime >= '{watermark}'" if watermark else q

    def pull(self, source_dir: Path, state: FeedState, stop: threading.Event) -> int:
        key = f"watermark:{self.cfg.folder_id}"
        watermark = state.get(self.feeder_id, key)
        params = {
            "q": self.query(watermark),
            "fields": FIELDS,
            "orderBy": "modifiedTime",
            "pageSize": "100",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        landed = 0
        high = watermark or ""
        while not stop.is_set():
            url = f"{FILES}?{urlencode(params)}"
            page = self._call(lambda h, u=url: self.http.get_json(u, h))
            for f in page.get("files") or []:
                if stop.is_set():
                    return landed
                fid, modified = str(f.get("id") or ""), str(f.get("modifiedTime") or "")
                mime = str(f.get("mimeType") or "")
                if not fid or not modified or not mime.startswith(("image/", "video/")):
                    continue
                high = max(high, modified)
                if state.known(self.feeder_id, fid, modified):
                    continue  # this version already landed (the watermark is inclusive)
                name = media_name(str(f.get("name") or fid), mime)
                if name is None or (f.get("capabilities") or {}).get("canDownload") is False:
                    log.info("gdrive: skipping %s (%s): not downloadable media", fid, mime)
                    continue
                try:
                    landed += self._file(f, name, source_dir, state, stop, {key: high})
                except _CannotDownload as exc:
                    log.warning(
                        "gdrive: skipping %s: the sign-in cannot download it (%s)", fid, exc
                    )
            token = page.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = str(token)
        return landed

    def _file(
        self,
        f: dict[str, Any],
        landing_name: str,
        source_dir: Path,
        state: FeedState,
        stop: threading.Event,
        cursor: dict[str, str],
    ) -> int:
        fid, modified, name = str(f["id"]), str(f["modifiedTime"]), str(f.get("name") or f["id"])
        url = f"{FILES}/{fid}?alt=media&supportsAllDrives=true"
        manifest = {
            "source": "gdrive",
            "source_id": fid,
            "drive_file_id": fid,
            "name": name,
            "modified_time": modified,
            "mime_type": f.get("mimeType"),
            "folder_id": self.cfg.folder_id,
            "md5": f.get("md5Checksum"),
        }
        target, size = land(
            source_dir,
            today(),
            landing_name,
            fid,
            lambda tmp: self._call(lambda h: self.http.download(url, h, tmp, stop), download=True),
            iso_ts(modified),
            manifest,
        )
        state.landed(self.feeder_id, fid, modified, f"{target.parent.name}/{target.name}", size,
                     cursor)  # fmt: skip
        return 1
