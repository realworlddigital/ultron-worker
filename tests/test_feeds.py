"""The Discord thread and Google Drive feeders: landing, cursors, registry reports, sync now."""

from __future__ import annotations

import dataclasses
import json
import threading
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from ultron_worker.config import (
    ConfigError,
    DiscordFeedConfig,
    DriveFeedConfig,
    Feeder,
    InboxConfig,
    load,
)
from ultron_worker.feeds import DiscordFeed, DriveFeed, FeedRunner
from ultron_worker.feeds.base import (
    FeedError,
    FeedState,
    FeedUnavailable,
    HttpError,
    ReadOnlyHttp,
    _SameHostAuth,
    land,
    media_name,
    safe_name,
)
from ultron_worker.feeds.base import today as base_today
from ultron_worker.inbox import InboxWatcher

from .conftest import PNG, FakeBackbone, FakeOllama

GUILD, THREAD = "111111111111111111", "222222222222222222"
FOLDER = "1AbCdEfGhIjKlMnOp"


class FakeHttp:
    """Every call the feeders make goes through these three methods (ReadOnlyHttp's surface)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.thread = {"id": THREAD, "guild_id": GUILD, "type": 11}
        self.messages: list[dict] = []
        self.files: list[dict] = []
        self.blobs: dict[str, bytes] = {}
        self.token_error: Exception | None = None
        self.fail: Exception | None = None

    def get_json(self, url: str, headers: dict) -> object:
        self.calls.append(("GET", url, headers))
        if self.fail:
            raise self.fail
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == f"/api/v10/channels/{THREAD}":
            return self.thread
        if u.path == f"/api/v10/channels/{THREAD}/messages":
            after, limit = int(q["after"]), int(q["limit"])
            newer = [m for m in self.messages if int(m["id"]) > after]
            return sorted(newer, key=lambda m: -int(m["id"]))[:limit]  # Discord: newest first
        if u.path == "/drive/v3/files":
            files = sorted(self.files, key=lambda f: f["modifiedTime"])
            wm = (
                q["q"].split("modifiedTime >= '")[1].rstrip("'") if "modifiedTime" in q["q"] else ""
            )
            return {"files": [f for f in files if f["modifiedTime"] >= wm]}
        raise AssertionError(f"unexpected GET {url}")

    def download(self, url: str, headers: dict, dest: Path, stop: threading.Event) -> int:
        self.calls.append(("DOWNLOAD", url, headers))
        if isinstance(self.blobs[url], Exception):
            raise self.blobs[url]
        data = self.blobs[url]
        dest.write_bytes(data)
        return len(data)

    def token(self, url: str, form: dict) -> dict:
        self.calls.append(("TOKEN", url, form))
        if self.token_error:
            raise self.token_error
        return {"access_token": "at-1", "expires_in": 3600}

    def gets(self, needle: str = "") -> list[str]:
        return [u for m, u, _ in self.calls if m == "GET" and needle in u]

    def downloads(self) -> list[str]:
        return [u for m, u, _ in self.calls if m == "DOWNLOAD"]


def msg(mid: int, *atts: tuple[str, str], author: str = "david") -> dict:
    return {
        "id": str(mid),
        "timestamp": "2026-11-23T15:04:05.000000+00:00",
        "author": {"id": "9", "username": author, "global_name": author.title()},
        "attachments": [
            {"id": f"{mid}{i}", "filename": name, "content_type": ctype,
             "url": f"https://cdn.discordapp.com/attachments/{mid}{i}/{name}"}
            for i, (name, ctype) in enumerate(atts)
        ],
    }  # fmt: skip


def drive_file(fid: str, name: str, mime: str, modified: str) -> dict:
    return {"id": fid, "name": name, "mimeType": mime, "modifiedTime": modified}


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    (tmp_path / "NAS/inbox").mkdir(parents=True)
    return tmp_path / "NAS"


@pytest.fixture
def auth(tmp_path: Path) -> Path:
    p = tmp_path / "gdrive-auth.json"
    p.write_text(json.dumps({"client_id": "c", "client_secret": "s", "refresh_token": "r"}))
    return p


@pytest.fixture
def feed_cfg(cfg, nas, auth):
    box = InboxConfig(
        enabled=True,
        root="NAS",
        settle_seconds=0,
        feeders=(Feeder("discord", "api", "discord", "Discord"),
                 Feeder("gdrive", "api", "gdrive", "Google Drive")),
    )  # fmt: skip
    return dataclasses.replace(
        cfg,
        media_roots=(("NAS", str(nas)),),
        media_inbox=box,
        discord_feed=DiscordFeedConfig(enabled=True, guild_id=GUILD, thread_id=THREAD),
        drive_feed=DriveFeedConfig(enabled=True, folder_id=FOLDER, auth_file=str(auth)),
    )


def setup(c, env: dict | None = None):
    bb = FakeBackbone(c)
    w = InboxWatcher(c, bb, FakeOllama())
    http = FakeHttp()
    env = {"ULTRON_DISCORD_TOKEN": "bot-token"} if env is None else env
    feeds = [DiscordFeed(c.discord_feed, http, env=env), DriveFeed(c.drive_feed, http)]
    runner = FeedRunner(c, w, feeds=feeds)
    w.register()
    w.flush()
    return runner, w, bb, http


def manifest(nas: Path, folder: str) -> list[dict]:
    path = nas / "inbox" / folder / base_today() / "manifest.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


# ---- Discord --------------------------------------------------------------------------------
def test_discord_without_a_token_reports_not_configured_once_and_calls_nothing(feed_cfg):
    runner, w, bb, http = setup(feed_cfg, env={})
    runner.tick(now=0)
    w.flush()
    report = bb.media["pull:discord"]
    assert report["ok"] is False and report["error"].startswith("not configured: no bot token")
    assert not http.gets("discord.com")
    calls = len(bb.media_calls)
    runner.tick(now=10_000)  # still unconfigured: reported once, not every cadence
    w.flush()
    assert [c for c in bb.media_calls[calls:] if c == ("pull", "discord")] == []


def test_discord_token_file_is_read_and_missing_thread_is_not_configured(feed_cfg, tmp_path):
    tok = tmp_path / "discord.token"
    tok.write_text("from-file\n")
    feed = DiscordFeed(dataclasses.replace(feed_cfg.discord_feed, token_file=str(tok)),
                       FakeHttp(), env={})  # fmt: skip
    assert feed.problem() is None and feed._token() == "from-file"
    feed = DiscordFeed(dataclasses.replace(feed_cfg.discord_feed, thread_id=""), FakeHttp(), env={})
    assert feed.problem() == "not configured: media_feeders.discord.thread_id not set"


def test_discord_lands_images_with_manifest_and_pulls_only_newer_messages(feed_cfg, nas):
    runner, w, bb, http = setup(feed_cfg)
    http.messages = [
        msg(1001, ("cat.png", "image/png"), ("notes.pdf", "application/pdf")),
        msg(1002, ("../../evil.jpg", "image/jpeg"), author="apollo"),
        msg(1003),  # text only
    ]
    for m in http.messages:
        for a in m["attachments"]:
            http.blobs[a["url"]] = PNG
    report = runner.pull("discord")
    assert report["ok"] and report["items"] == 2
    assert (report["items_24h"], report["items_total"]) == (2, 2)
    day = nas / "inbox/discord" / base_today()
    assert sorted(p.name for p in day.iterdir()) == ["cat.png", "evil.jpg", "manifest.jsonl"]
    lines = manifest(nas, "discord")
    assert [(x["source_id"], x["message_id"], x["author"]["username"]) for x in lines] == [
        ("10010", "1001", "david"),
        ("10020", "1002", "apollo"),
    ]
    assert lines[0]["timestamp"] == "2026-11-23T15:04:05.000000+00:00"
    assert all(x["file"] in ("cat.png", "evil.jpg") for x in lines)
    assert not any("pdf" in u for u in http.downloads())
    # CDN downloads never carry the bot token
    assert all("Authorization" not in h for m, _, h in http.calls if m == "DOWNLOAD")
    assert runner.state.get("discord", f"after:{THREAD}") == "1003"

    http.calls.clear()
    http.messages.append(msg(1004, ("dog.webp", "image/webp")))
    http.blobs[http.messages[-1]["attachments"][0]["url"]] = PNG
    assert runner.pull("discord")["items"] == 1
    assert http.gets("/messages") == [
        f"https://discord.com/api/v10/channels/{THREAD}/messages?after=1003&limit=100"
    ]
    assert http.downloads() == ["https://cdn.discordapp.com/attachments/10040/dog.webp"]
    assert runner.pull("discord")["items"] == 0  # nothing new: nothing downloaded again


def test_discord_never_redownloads_a_known_message_id(feed_cfg):
    runner, w, bb, http = setup(feed_cfg)
    http.messages = [msg(5, ("a.png", "image/png"))]
    http.blobs[http.messages[0]["attachments"][0]["url"]] = PNG
    assert runner.pull("discord")["items"] == 1
    runner.state.set("discord", f"after:{THREAD}", "0")  # even with the cursor lost
    http.calls.clear()
    assert runner.pull("discord")["items"] == 0
    assert http.downloads() == []


def test_discord_refuses_a_channel_outside_the_server_and_stops_on_401(feed_cfg):
    runner, w, bb, http = setup(feed_cfg)
    http.thread = {"id": THREAD, "guild_id": "999999999999999999", "type": 11}
    report = runner.pull("discord")
    assert not report["ok"] and "not a thread in server" in report["error"]
    http.fail = __import__("ultron_worker.feeds.base", fromlist=["HttpError"]).HttpError(401, "")
    assert "rejected" in runner.pull("discord")["error"]
    http.calls.clear()
    assert "rejected" in runner.pull("discord")["error"]
    assert http.calls == []  # stopped: no retry with the refused token


# ---- Google Drive ---------------------------------------------------------------------------
def test_drive_without_sign_in_or_folder_reports_not_configured(feed_cfg, auth):
    auth.unlink()
    runner, w, bb, http = setup(feed_cfg)
    report = runner.pull("gdrive")
    assert report["error"].startswith("not configured: no Google sign-in")
    assert http.calls == []
    feed = DriveFeed(dataclasses.replace(feed_cfg.drive_feed, folder_id=""), http)
    assert feed.problem() == "not configured: media_feeders.gdrive.folder_id not set"
    auth.write_text("{}")
    assert "not a Google authorized-user sign-in" in DriveFeed(feed_cfg.drive_feed, http).problem()


def test_drive_pulls_new_and_changed_media_with_a_watermark(feed_cfg, nas):
    runner, w, bb, http = setup(feed_cfg)
    http.files = [
        drive_file("f1", "beach.jpg", "image/jpeg", "2026-11-23T10:00:00.000Z"),
        drive_file("f2", "clip.mp4", "video/mp4", "2026-11-23T11:00:00.000Z"),
    ]
    for f in ("f1", "f2", "f3"):
        http.blobs[
            f"https://www.googleapis.com/drive/v3/files/{f}?alt=media&supportsAllDrives=true"
        ] = PNG
    report = runner.pull("gdrive")
    assert report["ok"] and report["items"] == 2
    q = parse_qs(urlparse(http.gets("/drive/v3/files")[0]).query)["q"][0]
    assert f"'{FOLDER}' in parents" in q and "trashed = false" in q
    assert "image/" in q and "video/" in q and "modifiedTime" not in q
    lines = manifest(nas, "gdrive")
    assert [(x["drive_file_id"], x["name"], x["modified_time"]) for x in lines] == [
        ("f1", "beach.jpg", "2026-11-23T10:00:00.000Z"),
        ("f2", "clip.mp4", "2026-11-23T11:00:00.000Z"),
    ]
    assert runner.state.get("gdrive", f"watermark:{FOLDER}") == "2026-11-23T11:00:00.000Z"
    assert all(h.get("Authorization") == "Bearer at-1" for m, _, h in http.calls if m != "TOKEN")

    http.calls.clear()
    http.files[0]["modifiedTime"] = "2026-11-23T12:00:00.000Z"  # f1 edited
    http.files.append(drive_file("f3", "beach.jpg", "image/jpeg", "2026-11-23T12:30:00.000Z"))
    assert runner.pull("gdrive")["items"] == 2
    q = parse_qs(urlparse(http.gets("/drive/v3/files")[0]).query)["q"][0]
    assert "modifiedTime >= '2026-11-23T11:00:00.000Z'" in q
    assert len(http.downloads()) == 2  # f2 (at the watermark, unchanged) is not fetched again
    names = sorted(p.name for p in (nas / "inbox/gdrive" / base_today()).iterdir())
    assert names == ["beach-f1.jpg", "beach-f3.jpg", "beach.jpg", "clip.mp4", "manifest.jsonl"]
    assert [t for t, *_ in http.calls].count("TOKEN") == 0  # the access token is reused


def test_drive_expired_sign_in_reports_error_and_stops_until_david_signs_in(feed_cfg, auth):
    from ultron_worker.feeds.base import HttpError

    runner, w, bb, http = setup(feed_cfg)
    http.token_error = HttpError(400, '{"error": "invalid_grant"}')
    report = runner.pull("gdrive")
    assert not report["ok"] and "expired or revoked" in report["error"]
    http.calls.clear()
    assert "expired or revoked" in runner.pull("gdrive")["error"]
    assert http.calls == []  # no retry, no credential improvisation
    import os

    auth.write_text(auth.read_text())
    st = auth.stat()
    os.utime(auth, (st.st_atime, st.st_mtime + 5))  # David signed in again
    http.token_error = None
    assert runner.pull("gdrive")["ok"]


# ---- registry + sync now --------------------------------------------------------------------
def test_cadence_reports_heartbeats_and_triggers_a_rescan(feed_cfg, nas):
    runner, w, bb, http = setup(feed_cfg)
    http.files = [drive_file("f1", "a.png", "image/png", "2026-11-23T10:00:00.000Z")]
    http.blobs["https://www.googleapis.com/drive/v3/files/f1?alt=media&supportsAllDrives=true"] = (
        PNG
    )
    runner.tick(now=0)
    w.flush()
    pull = bb.media["pull:gdrive"]
    assert pull["ok"] is True and pull["items_24h"] == 1 and pull["items_total"] == 1
    assert set(pull) <= {"ok", "pulled_at", "items_24h", "items_total", "error"}
    assert bb.media["pull:discord"]["ok"] is True
    assert w.rescan.is_set()
    http.calls.clear()
    runner.tick(now=60)  # not due yet
    assert http.calls == []
    res = w.run_pass()
    assert res.new["gdrive"] == 1  # the watcher ingests it; the manifest is not media


def test_paused_feeder_skips_cadence_pulls(feed_cfg):
    runner, w, bb, http = setup(feed_cfg)
    bb.paused.add("discord")
    runner.tick(now=0)
    assert http.gets("discord.com") == []
    assert http.gets("googleapis.com")


def test_sync_now_forces_an_immediate_pull_and_rescan(feed_cfg, nas):
    runner, w, bb, http = setup(feed_cfg)
    runner.tick(now=0)  # cadence: nothing yet
    http.files = [
        drive_file("f1", "batch1.jpg", "image/jpeg", "2026-11-23T10:00:00.000Z"),
        drive_file("f2", "batch2.jpg", "image/jpeg", "2026-11-23T10:00:01.000Z"),
    ]
    for f in ("f1", "f2"):
        http.blobs[
            f"https://www.googleapis.com/drive/v3/files/{f}?alt=media&supportsAllDrives=true"
        ] = PNG
    bb.sync_command("msc_1", "gdrive", "pull")
    w.handle_syncs()
    w.flush()
    assert bb.syncs["msc_1"]["status"] == "done"
    assert bb.syncs["msc_1"]["data"]["items"] == 2
    pull = bb.media["pull:gdrive"]
    assert pull["ok"] and pull["items_total"] == 2
    assert set(pull) <= {"ok", "pulled_at", "items_24h", "items_total", "error"}
    assert len(list((nas / "inbox/gdrive" / base_today()).glob("*.jpg"))) == 2


def test_sync_now_on_an_unconfigured_feeder_fails_with_the_reason(feed_cfg):
    runner, w, bb, http = setup(feed_cfg, env={})
    bb.sync_command("msc_2", "discord", "pull")
    w.handle_syncs()
    w.flush()
    assert bb.syncs["msc_2"]["status"] == "failed"
    assert "not configured" in bb.syncs["msc_2"]["data"]["error"]
    assert bb.media["pull:discord"]["ok"] is False


# ---- read-only + landing safety ---------------------------------------------------------------
def test_read_only_http_refuses_other_hosts_and_methods():
    http = ReadOnlyHttp()
    for url in (
        "http://discord.com/api/v10/x",
        "https://evil.example/x",
        "https://discord.com.evil/x",
    ):
        with pytest.raises(FeedError, match="refusing"):
            http.get_json(url, {})
    with pytest.raises(FeedError, match="refusing"):
        http.token("https://discord.com/api/v10/channels/1/messages", {})
    assert not any(hasattr(http, m) for m in ("post", "put", "patch", "delete"))


def test_redirects_never_carry_the_credential_to_another_host():
    req = urllib.request.Request("https://www.googleapis.com/a", headers={"Authorization": "x"})
    h = _SameHostAuth()
    new = h.redirect_request(req, None, 302, "Found", {}, "https://other.example/b")
    assert "Authorization" not in new.headers
    same = h.redirect_request(req, None, 302, "Found", {}, "https://www.googleapis.com/b")
    assert same.headers.get("Authorization") == "x"
    with pytest.raises(FeedError):
        h.redirect_request(req, None, 302, "Found", {}, "http://www.googleapis.com/b")


def test_safe_name_strips_paths_and_unsafe_characters():
    assert safe_name("../../etc/passwd.png") == "passwd.png"
    assert safe_name("C:\\x\\y<z>.jpg") == "y_z_.jpg"
    assert safe_name("...") == "file"
    assert len(safe_name("a" * 300 + ".jpeg")) <= 120


def test_feed_state_counts_and_cursor(tmp_path):
    st = FeedState(tmp_path / "s.sqlite3")
    st.landed("d", "1", "v", "x/a.png", 3, {"after:t": "5"})
    assert st.known("d", "1", "v") and not st.known("d", "1", "w")
    assert st.get("d", "after:t") == "5" and st.counts("d") == (1, 1) and st.counts("g") == (0, 0)


# ---- config -----------------------------------------------------------------------------------
def test_media_feeders_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "k")
    (tmp_path / "NAS").mkdir()
    base = f"""
[backbone]
url = "https://bb.example"
[runner]
allowed_roots = ["{tmp_path.as_posix()}"]
[media_roots]
NAS = "{tmp_path.as_posix()}/NAS"
[media_inbox]
enabled = true
root = "NAS"
"""
    p = tmp_path / "c.toml"
    p.write_text(base)
    assert load(p).api_feeds() == ()
    p.write_text(base + f"""
[media_feeders.discord]
enabled = true
guild_id = "{GUILD}"
thread_id = "{THREAD}"
[media_feeders.gdrive]
enabled = true
folder_id = "{FOLDER}"
auth_file = "~/gdrive.json"
""")  # fmt: skip
    c = load(p)
    assert [f.feeder_id for f in c.api_feeds()] == ["discord", "gdrive"]
    kinds = {f.feeder_id: (f.kind, f.folder) for f in c.media_inbox.feeders}
    assert kinds == {"discord": ("api", "discord"), "gdrive": ("api", "gdrive")}
    assert not c.drive_feed.auth_file.startswith("~")
    for bad in (
        '[media_feeders.discord]\nenabled = true\nthread_id = "general"\n',
        '[media_feeders.gdrive]\nenabled = true\nfolder_id = "x\' or 1=1"\n',
        "[media_feeders.gdrive]\nenabled = true\ninterval_seconds = 5\n",
        '[media_inbox.feeders.gdrive]\nkind = "native"\n[media_feeders.gdrive]\nenabled = true\n',
    ):
        p.write_text(base + bad)
        with pytest.raises(ConfigError):
            load(p)
    p.write_text(base.replace("enabled = true", "enabled = false")
                 + "[media_feeders.discord]\nenabled = true\n")  # fmt: skip
    with pytest.raises(ConfigError, match="needs \\[media_inbox\\]"):
        load(p)


def test_runner_starts_the_feeders_beside_the_watcher(feed_cfg, make):
    runner, bb, _ = make(config=feed_cfg)
    assert runner.feeds is not None and set(runner.feeds.feeds) == {"discord", "gdrive"}
    assert set(runner.inbox.pullers) == {"discord", "gdrive"}
    runner.heartbeat()
    assert "feeds" in bb.statuses[-1]["detail"]


def test_runner_without_api_feeders_has_no_feed_thread(cfg, make):
    runner, _, _ = make()
    assert runner.feeds is None


# ---- review fixes -----------------------------------------------------------------------------
def media_url(fid: str) -> str:
    return f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media&supportsAllDrives=true"


def test_a_short_download_is_a_failed_pull(tmp_path, monkeypatch):
    class Resp:
        headers = {"Content-Length": "10"}

        def __init__(self):
            self.parts = [b"12345", b""]

        def read(self, n):
            return self.parts.pop(0)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    http = ReadOnlyHttp()
    monkeypatch.setattr(http._opener, "open", lambda req, timeout: Resp())
    with pytest.raises(FeedUnavailable, match="short download"):
        http.download("https://cdn.discordapp.com/a.png", {}, tmp_path / "a", threading.Event())
    Resp.headers = {"Content-Length": "5"}
    assert (
        http.download("https://cdn.discordapp.com/a.png", {}, tmp_path / "b", threading.Event())
        == 5
    )


def test_land_refuses_a_linked_day_folder_or_manifest(tmp_path):
    src, outside = tmp_path / "inbox/gdrive", tmp_path / "outside"
    src.mkdir(parents=True)
    outside.mkdir()
    (src / "2026-11-23").symlink_to(outside, target_is_directory=True)
    fetch = lambda p: p.write_bytes(PNG) or len(PNG)  # noqa: E731
    with pytest.raises(FeedError, match="refusing"):
        land(src, "2026-11-23", "a.png", "f1", fetch, None, {})
    (src / "2026-11-24").mkdir()
    (src / "2026-11-24/manifest.jsonl").symlink_to(outside / "m.jsonl")
    with pytest.raises(FeedError, match="refusing"):
        land(src, "2026-11-24", "a.png", "f1", fetch, None, {})
    assert list(outside.iterdir()) == []


def test_drive_skips_a_file_the_sign_in_cannot_download(feed_cfg, nas):
    runner, w, bb, http = setup(feed_cfg)
    http.files = [
        drive_file("f1", "locked.jpg", "image/jpeg", "2026-11-23T10:00:00.000Z"),
        {**drive_file("f2", "nodl.jpg", "image/jpeg", "2026-11-23T10:30:00.000Z"),
         "capabilities": {"canDownload": False}},
        drive_file("f3", "open.jpg", "image/jpeg", "2026-11-23T11:00:00.000Z"),
    ]  # fmt: skip
    http.blobs[media_url("f1")] = HttpError(403, '{"reason": "cannotDownloadFile"}')
    http.blobs[media_url("f3")] = PNG
    report = runner.pull("gdrive")
    assert report["ok"] and report["items"] == 1
    assert media_url("f2") not in http.downloads()
    http.blobs[media_url("f1")] = HttpError(403, '{"reason": "rateLimitExceeded"}')
    http.files[0]["modifiedTime"] = "2026-11-23T12:00:00.000Z"
    assert not runner.pull("gdrive")["ok"]  # a real 403 still fails the pull


def test_landing_names_are_ones_the_watcher_ingests(feed_cfg, nas, monkeypatch):
    assert media_name("IMG_0001", "image/jpeg") == "IMG_0001.jpg"
    assert media_name("clip", "video/mp4") == "clip.mp4"
    assert media_name("pic.heic", "image/heic") is None
    monkeypatch.setattr("mimetypes.guess_extension", lambda *a, **k: None)
    assert media_name("IMG_2", "image/jpeg; charset=binary") == "IMG_2.jpg"
    assert media_name("mov", "video/quicktime") == "mov.mov"
    monkeypatch.undo()
    assert media_name("a.PNG", None) == "a.PNG"
    runner, w, bb, http = setup(feed_cfg)
    http.messages = [msg(7, ("photo.heic", "image/heic"), ("noext", "image/png"))]
    for a in http.messages[0]["attachments"]:
        http.blobs[a["url"]] = PNG
    assert runner.pull("discord")["items"] == 1
    assert [p.name for p in (nas / "inbox/discord" / base_today()).glob("*.png")] == ["noext.png"]
    assert not any("heic" in u for u in http.downloads())
