from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from ultron_worker import inbox as inbox_mod
from ultron_worker.backbone import ScopeViolation
from ultron_worker.config import ConfigError, Feeder, InboxConfig, load
from ultron_worker.inbox import InboxWatcher

from .conftest import PNG, FakeBackbone, FakeOllama

OLD = 1_700_000_000  # a fixed, long-settled mtime


def png(path: Path, salt: str, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PNG + salt.encode())  # distinct content after IEND
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    root = tmp_path / "NAS"
    png(root / "inbox/phone/2026-11-23/kitty.png", "A")
    png(root / "inbox/phone/2026-11-23/kitty2.png", "A")  # same bytes: a second arrival
    png(root / "inbox/Dropbox Camera/2026-11-22/dog.png", "B")
    png(root / "inbox/phone/2026-11-23/tree.png", "C")
    (root / "inbox/phone/2026-11-23/notes.txt").write_text("not media")
    return root


@pytest.fixture
def box_cfg(cfg, nas):
    box = InboxConfig(
        enabled=True,
        root="NAS",
        settle_seconds=0,
        feeders=(Feeder("phone", "native", "phone", "Phone"), Feeder("discord", "api", "discord")),
        subjects=(("Subjects/Cats", ("kitty",)), ("Subjects/Dogs", ("dog",))),
    )
    return dataclasses.replace(cfg, media_roots=(("NAS", str(nas)),), media_inbox=box)


def watcher(c, bb=None, ollama=None, **kw) -> tuple[InboxWatcher, FakeBackbone, FakeOllama]:
    bb = bb or FakeBackbone(c)
    ollama = ollama or FakeOllama()
    return InboxWatcher(c, bb, ollama, **kw), bb, ollama


def all_tags(nas: Path) -> list[dict]:
    lines = (nas / "_index/all-tags.jsonl").read_text().splitlines()
    return [json.loads(x) for x in lines]


def test_pass_inventories_dedupes_tags_files_and_reports(box_cfg, nas, tmp_path):
    w, bb, ollama = watcher(box_cfg)
    res = w.run_pass()
    assert res.status == "ok"
    assert dict(res.new) == {"phone": 2, "dropbox-camera": 1}
    assert res.duplicates == 1 and res.filed == 2 and res.review == 1
    assert len(ollama.calls) == 3 and {c["model"] for c in ollama.calls} == {"muse-glimmer:30b"}
    # filed into the subject folder (date kept), the inbox copy gone
    assert (nas / "Subjects/Cats/2026-11-23/kitty.png").is_file()
    assert not (nas / "inbox/phone/2026-11-23/kitty.png").exists()
    assert (nas / "Subjects/Dogs/2026-11-22/dog.png").is_file()
    # no subject: stays in the inbox, flagged; the duplicate stays too, noted not recorded
    assert (nas / "inbox/phone/2026-11-23/tree.png").is_file()
    assert (nas / "inbox/phone/2026-11-23/kitty2.png").is_file()
    recs = {r["relpath"]: r for r in all_tags(nas)}
    assert set(recs) == {
        "Subjects/Cats/2026-11-23/kitty.png",
        "Subjects/Dogs/2026-11-22/dog.png",
        "inbox/phone/2026-11-23/tree.png",
    }
    kitty = recs["Subjects/Cats/2026-11-23/kitty.png"]
    assert kitty["duplicates"][0]["path"] == "inbox/phone/2026-11-23/kitty2.png"
    assert kitty["size_bytes"] == len(PNG) + 1 and kitty["mtime"] == OLD
    assert recs["inbox/phone/2026-11-23/tree.png"]["review"] == "no subject folder matches the tags"
    assert "tags" in recs["inbox/phone/2026-11-23/tree.png"]
    for r in recs.values():
        assert "path" not in r and str(tmp_path) not in json.dumps(r)
    toc = [json.loads(x) for x in (nas / "_index/catalog.jsonl").read_text().splitlines()]
    assert toc[0]["folder"] == "inbox" and toc[0]["index"] == "_index/inbox-tags.jsonl"
    # backbone: feeders registered as ours, heartbeat counts what is still pending
    assert bb.media["feeders:phone"]["worker"] == "ultron-runner"
    assert bb.media["feeders:dropbox-camera"]["source_folder"] == "inbox/Dropbox Camera"
    beat = bb.media["ingest:ultron-inbox"]
    size = len(PNG) + 1
    assert beat["totals"] == {"photos_waiting": 1, "videos_waiting": 0, "bytes_waiting": size}
    sources = {s["feeder_id"]: s for s in beat["sources"]}
    assert set(sources) == {"phone", "discord", "dropbox-camera"}
    assert (
        sources["phone"]["photos_waiting"] == 1 and sources["dropbox-camera"]["bytes_waiting"] == 0
    )
    assert beat["last_run"]["status"] == "ok"
    items = [v for k, v in bb.media.items() if k.startswith("items:")]
    assert len(items) == 3  # one per distinct content
    filed = [i for i in items if i["tag_status"] == "filed"]
    assert {i["filed_to"] for i in filed} == {
        "Subjects/Cats/2026-11-23/kitty.png",
        "Subjects/Dogs/2026-11-22/dog.png",
    }
    assert all(i["root"] == "NAS" and not os.path.isabs(i["path"]) for i in items)
    # a native feeder's last pull is inferred from its newest file, never claimed as "now"
    assert bb.media["pull:phone"]["pulled_at"].startswith("2023-11-14")
    assert "pull:discord" not in bb.media
    assert w.db.queued() == []


def test_second_pass_adds_nothing_and_skips_rehash(box_cfg, nas, monkeypatch):
    w, bb, ollama = watcher(box_cfg)
    w.run_pass()
    calls = len(ollama.calls)
    hashed = []
    real = inbox_mod.sha256
    monkeypatch.setattr(inbox_mod, "sha256", lambda p, s=None: hashed.append(p) or real(p, s))
    res = w.run_pass()
    assert sum(res.new.values()) == 0 and res.duplicates == 0
    assert len(ollama.calls) == calls and hashed == []
    assert len(w.db.q("SELECT * FROM items")) == 3
    # the same bytes landing a third time: still one record, second arrival noted
    png(nas / "inbox/phone/2026-11-24/kitty3.png", "A")
    res = w.run_pass()
    assert res.duplicates == 1 and len(w.db.q("SELECT * FROM items")) == 3
    kitty = next(r for r in all_tags(nas) if r["relpath"].endswith("kitty.png"))
    assert len(kitty["duplicates"]) == 2


def test_unsettled_file_waits_for_next_pass(box_cfg, nas):
    c = dataclasses.replace(
        box_cfg, media_inbox=dataclasses.replace(box_cfg.media_inbox, settle_seconds=600)
    )
    png(nas / "inbox/phone/2026-11-24/landing.png", "D", mtime=__import__("time").time())
    w, _, _ = watcher(c)
    w.run_pass()
    assert (nas / "inbox/phone/2026-11-24/landing.png").is_file()
    assert not w.db.q("SELECT * FROM items WHERE rel LIKE '%landing%'")


class Garbage(FakeOllama):
    def generate(self, model, prompt, **kw):
        super().generate(model, prompt)
        return "I cannot tell"


def test_untaggable_item_stays_flagged_never_dropped(box_cfg, nas):
    c = dataclasses.replace(
        box_cfg, media_inbox=dataclasses.replace(box_cfg.media_inbox, review_after_attempts=2)
    )
    w, bb, _ = watcher(c, ollama=Garbage())
    w.run_pass()
    assert w.db.q("SELECT COUNT(*) n FROM items WHERE status='waiting'")[0]["n"] == 3
    w.run_pass()
    assert w.db.q("SELECT COUNT(*) n FROM items WHERE status='review'")[0]["n"] == 3
    assert (nas / "inbox/phone/2026-11-23/kitty.png").is_file()
    recs = all_tags(nas)
    assert len(recs) == 3
    assert all(r["failed"].startswith("tags could not be inferred") and "tags" not in r
               for r in recs)  # fmt: skip
    assert bb.media["ingest:ultron-inbox"]["totals"]["photos_waiting"] == 3


def test_model_down_is_an_error_pass_and_items_wait(box_cfg, nas):
    ollama = FakeOllama()
    ollama.down = True
    w, bb, _ = watcher(box_cfg, ollama=ollama)
    res = w.run_pass()
    assert res.status == "error" and "vision model unavailable" in res.error
    beat = bb.media["ingest:ultron-inbox"]
    assert beat["last_run"]["status"] == "error" and beat["totals"]["photos_waiting"] == 3
    assert w.db.q("SELECT COUNT(*) n FROM items WHERE status='waiting'")[0]["n"] == 3


def test_missing_inbox_reports_error_not_zero_silence(box_cfg, nas):
    import shutil

    shutil.rmtree(nas / "inbox")
    w, bb, _ = watcher(box_cfg)
    res = w.run_pass()
    assert res.status == "error" and "not found" in res.error
    assert bb.media["ingest:ultron-inbox"]["last_run"]["status"] == "error"


def test_outage_queues_every_write_and_restart_replays(box_cfg, nas):
    w, bb, _ = watcher(box_cfg)
    bb.down = True
    w.run_pass()
    assert bb.media == {}
    queued = len(w.db.queued())
    assert queued >= 6  # feeders, items, heartbeat, inferred pulls
    w.run_pass()  # a second pass coalesces into the same rows
    assert len(w.db.queued()) == queued
    bb.down = False
    w2, _, _ = watcher(box_cfg, bb=bb)  # a restart over the same state dir
    assert w2.flush() == queued
    assert bb.media["ingest:ultron-inbox"]["totals"]["photos_waiting"] == 1
    assert w2.db.queued() == []


def test_busy_runner_counts_but_does_not_tag(box_cfg, nas):
    w, bb, ollama = watcher(box_cfg, busy=lambda: True)
    w.run_pass()
    assert ollama.calls == []
    assert bb.media["ingest:ultron-inbox"]["totals"]["photos_waiting"] == 3


def test_interrupted_move_is_finished_on_the_next_pass(box_cfg, nas, monkeypatch):
    w, _, _ = watcher(box_cfg)

    def killed(self, h):
        raise KeyboardInterrupt  # a hard stop right after the intent was recorded

    monkeypatch.setattr(InboxWatcher, "_move", killed)
    with pytest.raises(KeyboardInterrupt):
        w.run_pass()
    assert w.db.q("SELECT COUNT(*) n FROM items WHERE status='filing'")[0]["n"] == 1
    monkeypatch.undo()
    w2, _, _ = watcher(box_cfg)
    w2.run_pass()
    assert w2.db.q("SELECT COUNT(*) n FROM items WHERE status='filing'")[0]["n"] == 0
    assert (nas / "Subjects/Cats/2026-11-23/kitty.png").is_file()
    assert not (nas / "inbox/phone/2026-11-23/kitty.png").exists()


def test_native_sync_rescans_and_acks_without_claiming_a_pull(box_cfg, nas):
    w, bb, _ = watcher(box_cfg)
    w.run_pass()
    png(nas / "inbox/phone/2026-11-24/new.png", "E")
    bb.sync_command("msc_1", "phone")
    bb.sync_command("msc_2", "someone-elses")
    assert w.handle_syncs() == 1
    assert bb.syncs["msc_1"]["status"] == "done" and bb.syncs["msc_1"]["data"]["items"] == 1
    assert bb.syncs["msc_2"]["status"] == "pending"  # not ours: never acked
    assert ("ack", "msc_1") in bb.media_calls
    assert bb.media["pull:phone"]["pulled_at"].startswith("2023-11-14")


def test_api_sync_runs_the_puller_or_fails_honestly(box_cfg, nas, monkeypatch):
    w, bb, _ = watcher(box_cfg)
    bb.sync_command("msc_1", "discord", "pull")
    w.handle_syncs()
    assert bb.syncs["msc_1"]["status"] == "failed"
    assert "no puller" in bb.syncs["msc_1"]["data"]["error"]

    def pull(folder: Path) -> int:
        png(folder / "2026-11-24/drop.png", "F")
        return 1

    monkeypatch.setitem(inbox_mod.PULLERS, "discord", pull)
    bb.sync_command("msc_2", "discord", "pull")
    w.handle_syncs()
    assert bb.syncs["msc_2"]["status"] == "done" and bb.syncs["msc_2"]["data"]["items"] == 1
    assert bb.media["pull:discord"]["ok"] is True
    assert w.db.q("SELECT feeder FROM items WHERE rel LIKE '%drop.png'")[0]["feeder"] == "discord"


def test_acked_command_left_by_a_kill_is_finished(box_cfg, nas):
    w, bb, _ = watcher(box_cfg)
    bb.sync_command("msc_1", "phone")
    bb.syncs["msc_1"].update(status="acked")
    bb.syncs["msc_1"]["data"]["acked_by"] = "ultron-runner"
    w.handle_syncs()
    assert bb.syncs["msc_1"]["status"] == "done"
    assert ("ack", "msc_1") not in bb.media_calls


def test_queued_outcome_is_delivered_not_rerun(box_cfg, nas):
    w, bb, ollama = watcher(box_cfg)
    bb.sync_command("msc_1", "phone")
    bb.syncs["msc_1"].update(status="acked")
    bb.syncs["msc_1"]["data"]["acked_by"] = "ultron-runner"
    w.db.put("finish:msc_1", "media_sync_finish", "msc_1", {"ok": True, "items": 7, "error": None})
    bb.down = True
    assert w.handle_syncs() == 0  # backbone down: nothing runs, the outcome stays queued
    bb.down = False
    w.handle_syncs()
    assert bb.syncs["msc_1"]["status"] == "done" and bb.syncs["msc_1"]["data"]["items"] == 7
    assert ollama.calls == []  # no second re-scan


def test_symlinked_files_are_never_followed(box_cfg, nas, tmp_path):
    outside = png(tmp_path / "private/secret.png", "S")
    (nas / "inbox/phone/2026-11-23/link.png").symlink_to(outside)
    (nas / "inbox/linked").symlink_to(outside.parent, target_is_directory=True)
    w, _, _ = watcher(box_cfg, busy=lambda: True)
    w.run_pass()
    rels = {r["rel"] for r in w.db.q("SELECT rel FROM items")}
    assert not any("link" in r or "secret" in r for r in rels)


def test_replaced_file_retires_the_old_item(box_cfg, nas):
    w, bb, _ = watcher(box_cfg, busy=lambda: True)
    w.run_pass()
    png(nas / "inbox/phone/2026-11-23/tree.png", "D", mtime=OLD + 5)  # new bytes, same name
    w.run_pass()
    rows = w.db.q("SELECT status FROM items WHERE rel='inbox/phone/2026-11-23/tree.png'")
    assert sorted(r["status"] for r in rows) == ["missing", "waiting"]
    assert bb.media["ingest:ultron-inbox"]["totals"]["photos_waiting"] == 3


def test_filing_never_overwrites_other_media(box_cfg, nas):
    w, _, _ = watcher(box_cfg, busy=lambda: True)
    w.run_pass()
    h = w.db.q("SELECT hash FROM items WHERE rel LIKE '%/kitty.png'")[0]["hash"]
    taken = [nas / "Subjects/Cats/2026-11-23/kitty.png",
             nas / f"Subjects/Cats/2026-11-23/kitty-{h[:8]}.png"]  # fmt: skip
    for i, t in enumerate(taken):
        png(t, f"other{i}")
    w.busy = lambda: False
    w.run_pass()
    assert (nas / f"Subjects/Cats/2026-11-23/kitty-{h[:8]}-2.png").is_file()
    assert [t.read_bytes()[-6:] for t in taken] == [b"other0", b"other1"]


def test_media_scope_is_the_worker_half_only(box_cfg):
    bb = FakeBackbone(box_cfg)
    with pytest.raises(ScopeViolation):
        bb.media_feeder("phone", {"display_name": "x", "kind": "native",
                                  "source_folder": "inbox/x", "worker": "apollo"})  # fmt: skip
    for method, path in (
        ("POST", "/media/feeders/phone/pause"),
        ("POST", "/media/feeders/phone/sync"),
        ("POST", "/media/items/abc/decision"),
    ):
        with pytest.raises(ScopeViolation):
            bb.request(method, path, body={})
    with pytest.raises(ScopeViolation):
        bb.request("GET", "/media/sync", params={"status": "done"})


def test_runner_heartbeat_carries_inbox_summary(make, box_cfg):
    runner, bb, _ = make(config=box_cfg)
    assert runner.inbox is not None
    runner.inbox.run_pass()
    runner.heartbeat()
    detail = bb.statuses[-1]["detail"]
    assert detail["inbox"]["status"] == "ok" and detail["inbox"]["pending"]["photos_waiting"] == 1


def test_config_media_inbox(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "k")
    base = f"""
[backbone]
url = "https://bb.example"
[runner]
allowed_roots = ["{tmp_path.as_posix()}"]
[media_roots]
NAS = "{tmp_path.as_posix()}/NAS"
"""
    p = tmp_path / "c.toml"
    p.write_text(base)
    assert load(p).media_inbox.enabled is False
    p.write_text(
        base
        + """
[media_inbox]
enabled = true
root = "NAS"
[media_inbox.feeders.phone]
display_name = "Phone"
[media_inbox.feeders.discord]
kind = "api"
[media_inbox.subjects]
"Brands/Guild" = ["Guild", "logo"]
"""
    )
    box = load(p).media_inbox
    assert box.model == "muse-glimmer:30b" and box.interval_seconds == 300
    assert {f.feeder_id: (f.kind, f.folder) for f in box.feeders} == {
        "phone": ("native", "phone"),
        "discord": ("api", "discord"),
    }
    assert box.subjects == (("Brands/Guild", ("guild", "logo")),)
    for bad in ('root = "NOPE"', 'subjects = {"inbox/x" = ["a"]}', 'inbox = "../up"'):
        p.write_text(base + f"[media_inbox]\nenabled = true\nroot = \"NAS\"\n{bad}\n"
                     .replace('root = "NAS"\nroot', "root"))  # fmt: skip
        with pytest.raises(ConfigError):
            load(p)
