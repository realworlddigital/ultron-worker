"""catalog_publish: the NAS catalog (TOC + folder tags.jsonl) published as record.media_asset."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ultron_worker import catalog
from ultron_worker.backbone import Backbone, ScopeViolation
from ultron_worker.jobs import publish
from ultron_worker.jobs.publish import _merge, asset_id

from .conftest import PNG

ROOT = "GUILD-RAW-Media"
NATICK = "Guild Hall/2026-10-GH NATICK"
CLIP = b"v" * 4000


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    """NATICK with a clip (hash already in its record), two stills with the same bytes (one
    asset, two locations), a second clip with technical metadata and clearance, an untagged
    still, and the TOC listing the folder index."""
    base = tmp_path / "nas" / ROOT
    folder = base / NATICK
    folder.mkdir(parents=True)
    files = {"C001.MP4": CLIP, "still.jpg": PNG, "CARD2/still copy.jpg": PNG, "C002.mov": b"w" * 99,
             "broken.png": PNG}  # fmt: skip
    records = []
    for rel, data in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_bytes(data)
        records.append(
            {"root": ROOT, "relpath": f"{NATICK}/{rel}", **catalog.file_stamp(folder / rel),
             "media": "video" if rel.lower().endswith((".mp4", ".mov")) else "image",
             "caption": f"caption of {rel}", "tags": ["camp", "Kids", "camp fire"]}
        )  # fmt: skip
    records[0]["content_hash"] = _sha(CLIP)
    records[3].update(technical={"duration_sec": 12.5, "width": 1920, "height": 1080,
                                 "codec": "h264", "fps": 29.97}, clearance="cleared")  # fmt: skip
    records[4] = {k: v for k, v in records[4].items() if k != "tags"} | {"failed": "bad image"}
    (folder / "tags.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (base / "_index").mkdir()
    (base / "_index" / "catalog.jsonl").write_text(
        json.dumps(
            {
                "root": ROOT,
                "folder": NATICK,
                "index": f"{NATICK}/tags.jsonl",
                "files": 5,
                "tagged": 4,
                "skipped": 0,
                "failed": 1,
            }
        )
        + "\n"  # fmt: skip
    )
    return base


@pytest.fixture
def hashes(monkeypatch) -> list[Path]:
    seen: list[Path] = []
    real = publish.sha256

    def counting(path, stop=None):
        seen.append(Path(path))
        return real(path, stop)

    monkeypatch.setattr(publish, "sha256", counting)
    monkeypatch.setattr(
        publish, "_probe", lambda path: ({"width": 1, "height": 1, "codec": "png"}, True)
    )
    return seen


@pytest.fixture
def run(make, cfg, raw, tmp_path, hashes):
    """run(tid, bb=None, status="done", **params) -> (bb, finished note, report)."""
    config = replace(cfg, media_roots=((ROOT, str(raw)),))
    state: dict = {}

    def _run(tid="t1", *, status="done", inputs=None, output=None, ticks=1, prep=None, add=True,
             **params):  # fmt: skip
        runner, bb, _ = make(bb=state.get("bb"), config=config)
        state["bb"] = bb
        if prep:
            prep(bb)
        out = output or tmp_path / "out" / f"{tid}.json"
        if add:
            bb.add(tid, kind="catalog_publish", model=None, inputs=inputs or [f"{ROOT}/{NATICK}"],
                   output=str(out), params=params)  # fmt: skip
        for _ in range(ticks):
            runner.tick()
        assert bb.tasks[tid]["status"] == status, bb.events
        notes = [n["body"] for t, n in bb.notes if t == tid and n["body"].startswith("Ultron fin")]
        report = json.loads(Path(out).read_text()) if status == "done" else None
        return bb, (notes[-1] if notes else None), report

    return _run


ORGS = {f"{ROOT}/{NATICK}": "guild_hall"}


def _index(raw: Path) -> list[dict]:
    return [json.loads(x) for x in (raw / NATICK / "tags.jsonl").read_text().splitlines()]


def test_publishes_tagged_entries_as_media_assets(run, raw, hashes) -> None:
    bb, note, report = run(orgs=ORGS)
    assert len(bb.assets) == 3  # the two identical stills are one asset
    clip = bb.assets[asset_id(_sha(CLIP))]["data"]
    assert clip["kind"] == "video" and clip["org"] == "guild_hall"
    assert clip["rights"] == "needs_check"
    assert clip["provenance"] | {"ingested_at": None} == {
        "feeder": "nas_mirror", "ingested_by": "ultron-runner", "ingested_at": None,
        "source_ref": "t1",
    }  # fmt: skip
    [loc] = clip["locations"]
    assert loc["storage_id"] == "nas-main" and loc["status"] == "verified"
    assert loc["path"] == f"{ROOT}/{NATICK}/C001.MP4" and loc["last_verified_at"]
    assert clip["tags"]["flat"] == ["camp", "kids", "camp fire"]
    assert clip["tags"]["structured"] == {
        "caption": "caption of C001.MP4", "keywords": ["camp", "Kids", "camp fire"],
        "folder": NATICK,
    }  # fmt: skip
    assert clip["technical"] == {"width": 1, "height": 1, "codec": "png", "size_bytes": len(CLIP)}
    still = bb.assets[asset_id(_sha(PNG))]["data"]
    assert [x["path"] for x in still["locations"]] == [
        f"{ROOT}/{NATICK}/still.jpg", f"{ROOT}/{NATICK}/CARD2/still copy.jpg"
    ]  # fmt: skip
    c002 = bb.assets[asset_id(_sha(b"w" * 99))]["data"]
    assert c002["rights"] == "cleared"
    assert c002["technical"] == {"duration_sec": 12.5, "fps": 29.97, "width": 1920,
                                 "height": 1080, "size_bytes": 99, "codec": "h264"}  # fmt: skip
    # hashed once each (the clip already had its hash), written back to the folder index
    assert sorted(p.name for p in hashes) == ["C002.mov", "still copy.jpg", "still.jpg"]
    recs = {r["relpath"].rsplit("/", 1)[1]: r for r in _index(raw)}
    assert recs["still.jpg"]["content_hash"] == _sha(PNG)
    assert recs["C002.mov"]["content_hash"] == _sha(b"w" * 99)
    assert recs["still.jpg"]["technical"] == {"width": 1, "height": 1, "codec": "png"}
    assert recs["C002.mov"]["technical"]["codec"] == "h264"  # a record's own metadata stays
    assert "content_hash" not in recs["broken.png"]
    folder = f"{ROOT}/{NATICK}"
    want = {"created": 3, "updated": 0, "skipped": 0, "duplicates": 1, "held": 0, "failed": 0}
    assert report["folders"][folder] == {**want, "hashed": 3}
    assert report["distinct_assets"] == 3 and report["task_id"] == "t1"
    assert "3 created, 0 updated, 0 skipped, 1 duplicates collapsed" in note
    assert "3 distinct asset(s)" in note and "1 untagged not published" in note


def test_second_identical_job_creates_nothing_and_hashes_nothing(run, hashes) -> None:
    bb, _, _ = run(orgs=ORGS)
    before = json.loads(json.dumps(bb.assets))
    hashes.clear()
    puts = sum(1 for m, p in bb.sent if p == "/media/assets")
    bb, note, report = run("t2", orgs=ORGS)
    assert bb.assets == before
    assert hashes == []
    assert sum(1 for m, p in bb.sent if p == "/media/assets") == puts  # all skipped: no writes
    assert report["totals"] | {"hashed": 0} == {"created": 0, "updated": 0, "skipped": 3,
                                                "duplicates": 1, "held": 0, "failed": 0,
                                                "hashed": 0}  # fmt: skip
    assert "0 created" in note and "3 distinct asset(s)" in note


def test_newer_local_entry_updates_and_keeps_first_publish(run, raw) -> None:
    bb, _, _ = run(orgs=ORGS)
    recs = _index(raw)
    recs[0]["tags"].append("lake")
    recs[0]["clearance"] = {"status": "blocked"}
    (raw / NATICK / "tags.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    bb, note, report = run("t2", orgs={f"{ROOT}/Guild Hall": "gcs"})
    doc = bb.assets[asset_id(_sha(CLIP))]
    assert doc["version"] == 2
    assert doc["data"]["tags"]["flat"] == ["camp", "kids", "camp fire", "lake"]
    assert doc["data"]["rights"] == "blocked"  # off needs_check
    assert doc["data"]["org"] == "guild_hall"  # first publish's org kept, and said so
    assert doc["data"]["provenance"]["source_ref"] == "t1"
    assert report["totals"]["updated"] == 1 and report["totals"]["skipped"] == 2
    assert "Already published under another org (kept): 4" in note


def test_without_an_org_mapping_entries_are_held_not_guessed(run, raw) -> None:
    bb, note, report = run()
    assert bb.assets == {}
    assert not [p for _, p in bb.sent if p == "/media/assets"]
    assert report["totals"]["held"] == 4
    assert "Held, no backbone org" in note and f"{ROOT}/{NATICK}/still.jpg" in note
    assert _index(raw)[1]["content_hash"] == _sha(PNG)  # the hashing isn't wasted


def test_outage_parks_with_progress_then_resumes(run, raw, hashes) -> None:
    def outage(bb):
        bb.asset_down_after = 2  # GET+PUT for the clip, then the still's GET fails

    bb, note, _ = run(status="in_progress", prep=outage, orgs=ORGS)
    assert len(bb.assets) == 1 and note is None
    assert any("backbone unreachable mid-publish" in e.get("summary", "") for e in bb.events)
    assert _index(raw)[1]["content_hash"] == _sha(PNG)  # flushed before parking
    hashed = len(hashes)

    def restore(bb):
        bb.asset_down_after, bb.assets_down = None, False

    bb, note, report = run(prep=restore, add=False, ticks=3)
    assert report["totals"]["created"] == 3 and report["totals"]["failed"] == 0
    # clip; then still, its duplicate's location, C002: the clip isn't written again
    assert sum(1 for _, p in bb.sent if p == "/media/assets") == 4
    assert len(hashes) - hashed == 2  # the still hashed before the outage isn't hashed again


def test_per_item_failures_are_counted_and_named(run, raw) -> None:
    (raw / NATICK / "C002.mov").write_bytes(b"w" * 100)  # changed since tagged

    def reject(bb):
        bb.asset_reject.add(_sha(CLIP))

    bb, note, report = run(prep=reject, orgs=ORGS)
    assert len(bb.assets) == 1
    assert report["totals"]["failed"] == 2 and report["totals"]["created"] == 1
    assert "Not published (failed): 2:" in note
    assert f"{ROOT}/{NATICK}/C001.MP4 (backbone 422" in note
    assert f"{ROOT}/{NATICK}/C002.mov (file changed since it was tagged" in note


@pytest.mark.parametrize(
    ("params", "output", "why"),
    [
        ({"org": "Guild Hall"}, None, "params.org='Guild Hall'"),
        ({"orgs": {"elsewhere": "gcs"}}, None, "params.orgs folder"),
        ({}, "report.jsonl", "must be a .json report file"),
    ],
)
def test_bad_specs_go_to_david(run, tmp_path, params, output, why) -> None:
    out = tmp_path / "out" / output if output else None
    bb, _, _ = run(status="needs_david", output=out, **params)
    [failed] = [e for e in bb.events if e["action"] == "ultron.failed"]
    assert why in failed["summary"]


def test_folder_without_an_index_is_refused(run, raw) -> None:
    (raw / "Dump").mkdir()
    bb, _, _ = run(status="needs_david", inputs=[f"{ROOT}/Dump"], orgs=ORGS)
    assert any("no folder index" in e["summary"] for e in bb.events)


def test_client_scope_for_media_assets(cfg) -> None:
    bb = Backbone(cfg)
    with pytest.raises(ScopeViolation):
        bb._check("GET", "/records/rec_123", None, None)
    with pytest.raises(ScopeViolation):
        bb._check("PUT", "/media/assets", None, {"kind": "image"})
    with pytest.raises(ScopeViolation):
        bb._check("DELETE", f"/records/{asset_id('sha256:00')}", None, None)
    bb._check("GET", f"/records/{asset_id('sha256:00')}", None, None)
    bb._check("PUT", "/media/assets", None, {"content_hash": "sha256:00"})


def test_merge_only_adds_and_replays_to_no_change() -> None:
    cur = {
        "content_hash": "sha256:aa", "kind": "image", "rights": "needs_check", "org": "gea",
        "tags": {"structured": {"keywords": ["a"]}, "flat": ["a"]},
        "technical": {"width": 10, "height": None},
        "provenance": {"feeder": "nas_mirror", "ingested_by": "ultron-runner",
                       "ingested_at": "2026-11-24T00:00:00+00:00", "source_ref": "t0"},
        "locations": [{"storage_id": "nas-main", "path": "R/x.jpg", "status": "verified",
                       "last_verified_at": publish._now()}],
        "embedding": None,
    }  # fmt: skip
    ours = {
        "content_hash": "sha256:aa", "kind": "image", "rights": "cleared", "org": "gea",
        "tags": {"structured": {"keywords": ["b"], "caption": "c"}, "flat": ["b"]},
        "technical": {"width": 99, "height": 20},
        "provenance": {"feeder": "nas_mirror", "ingested_by": "ultron", "ingested_at": "now",
                       "source_ref": "t1"},
        "locations": [{"storage_id": "nas-main", "path": "R/x.jpg", "status": "verified",
                       "last_verified_at": "later"}],
    }  # fmt: skip
    merged, notes = _merge(cur, ours)
    assert notes == {}
    assert merged["tags"] == {"structured": {"keywords": ["a", "b"], "caption": "c"},
                              "flat": ["a", "b"]}  # fmt: skip
    assert merged["technical"] == {"width": 10, "height": 20}
    assert merged["rights"] == "cleared"
    assert merged["provenance"] == cur["provenance"] and merged["locations"] == cur["locations"]
    assert _merge(merged, ours)[0] == merged


def test_probe_without_ffprobe_publishes_size_only(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(publish.shutil, "which", lambda name: None)
    assert publish._probe(tmp_path / "x.mp4") == ({}, False)


def _toc_add(raw: Path, index: str) -> None:
    with (raw / "_index" / "catalog.jsonl").open("a") as fh:
        fh.write(
            json.dumps({"root": ROOT, "folder": index.rsplit("/", 1)[0], "index": index}) + "\n"
        )


def test_tagged_child_record_wins_and_other_roots_stay_out(run, raw) -> None:
    recs = _index(raw)
    still = dict(recs[1])
    recs[1] = {k: v for k, v in recs[1].items() if k != "tags"} | {"failed": "timeout"}
    (raw / NATICK / "tags.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
    stray = still | {"root": "OTHER-Media", "relpath": f"{NATICK}/stray.jpg"}
    (raw / NATICK / "CARD2" / "tags.jsonl").write_text(
        json.dumps(still) + "\n" + json.dumps(stray) + "\n"
    )
    _toc_add(raw, f"{NATICK}/CARD2/tags.jsonl")
    bb, _, report = run(orgs=ORGS)
    assert report["totals"]["created"] == 3 and report["totals"]["failed"] == 0
    published = json.dumps(bb.assets)
    assert f"{NATICK}/still.jpg" in published and "stray.jpg" not in published


def test_report_target_that_is_a_folder_index_is_refused(run, raw) -> None:
    index = raw / NATICK / "CARD2" / "tags.json"
    index.write_text('{"items": []}\n')
    _toc_add(raw, f"{NATICK}/CARD2/tags.json")
    bb, _, _ = run(status="needs_david", output=index, orgs=ORGS)
    assert any("is a folder index" in e.get("summary", "") for e in bb.events)
    assert index.read_text() == '{"items": []}\n'


def test_entry_problems_fail_their_entry_not_the_job(run, raw, monkeypatch) -> None:
    from ultron_worker import runner

    monkeypatch.setattr(runner, "BROKEN_AFTER", 2)
    (raw / NATICK / "C001.MP4").unlink()
    (raw / NATICK / "still.jpg").unlink()
    bb, note, report = run(orgs=ORGS)
    assert report["totals"]["failed"] == 2 and report["totals"]["created"] == 2
    assert f"{ROOT}/{NATICK}/still.jpg (file missing on disk)" in note


def test_a_hash_is_not_written_onto_a_replaced_file(raw) -> None:
    from types import SimpleNamespace

    from ultron_worker.media import MediaRoots

    ctx = SimpleNamespace(cache={}, media_roots=MediaRoots({ROOT: str(raw)}))
    index = raw / NATICK / "tags.jsonl"
    rec = _index(raw)[1]
    key = ctx.media_roots.identity(ctx.media_roots.normalize(rec))
    stale = {"size_bytes": rec["size_bytes"] + 1, "mtime": rec["mtime"]}
    publish._queue(ctx, index, key, {"content_hash": _sha(b"old"), "stamp": stale})
    publish._flush(ctx, index)
    assert "content_hash" not in _index(raw)[1]
    current = {"size_bytes": rec["size_bytes"], "mtime": rec["mtime"]}
    publish._queue(ctx, index, key, {"content_hash": _sha(PNG), "stamp": current})
    publish._flush(ctx, index)
    assert _index(raw)[1]["content_hash"] == _sha(PNG)
