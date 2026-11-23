"""folder_inventory: a model-free walk of media folders and the unindexed diff against the
catalog TOC (``<root>/_index/catalog.jsonl``) and the folder indexes it lists."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from ultron_worker.jobs.inventory import _fit

from .conftest import PNG

NATICK = "Guild Hall/2026-10-GH NATICK"


@pytest.fixture
def raw(tmp_path: Path) -> Path:
    base = tmp_path / "nas" / "GUILD-RAW-Media"
    files = {
        f"{NATICK}/C001.MP4": b"v" * 10,
        f"{NATICK}/C002.mov": b"v" * 20,
        f"{NATICK}/still.jpg": PNG,
        f"{NATICK}/notes.txt": b"not media",
        f"{NATICK}/CARD2/frame.png": PNG,
        "Dump/clip.mkv": b"v" * 5,
        "Dump/pic.webp": PNG,
        "Dump/readme.md": b"no",
        "@eaDir/SYNOFILE_THUMB.jpg": PNG,
        "top.jpg": PNG,
    }
    for rel, data in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_bytes(data)
    return base


@pytest.fixture
def run(make, cfg, raw, tmp_path):
    """run(inputs, **params) -> (status, finished-note body, output records, sidecar)."""
    config = replace(cfg, media_roots=(("GUILD-RAW-Media", str(raw)),))

    def _run(inputs, *, status="done", **params):
        runner, bb, ollama = make(config=config)
        out = tmp_path / "out" / "inventory.jsonl"
        bb.add("t1", kind="folder_inventory", model=None, inputs=inputs, output=str(out),
               params=params)  # fmt: skip
        runner.tick()
        assert bb.tasks["t1"]["status"] == status, bb.events
        assert ollama.calls == []  # never touches a model
        if status != "done":
            return None, [e for e in bb.events if e["action"] == "ultron.failed"], None, None
        [note] = [n["body"] for _, n in bb.notes if n["body"].startswith("Ultron finished")]
        rows = [json.loads(x) for x in out.read_text().splitlines()]
        side = json.loads((out.parent / "inventory.unindexed.json").read_text())
        return status, note, rows, side

    return _run


def _catalog(raw: Path, entries: list[dict], indexes: dict[str, list[dict]]) -> None:
    (raw / "_index").mkdir(exist_ok=True)
    (raw / "_index" / "catalog.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    for rel, records in indexes.items():
        (raw / rel).write_text("".join(json.dumps(r) + "\n" for r in records))


def test_walk_emits_one_record_per_media_file(run, raw) -> None:
    _, note, rows, side = run([str(raw)])
    assert [(r["rel"], r["media"]) for r in rows] == [
        ("top.jpg", "image"),
        ("Dump/clip.mkv", "video"),
        ("Dump/pic.webp", "image"),
        (f"{NATICK}/C001.MP4", "video"),
        (f"{NATICK}/C002.mov", "video"),
        (f"{NATICK}/still.jpg", "image"),
        (f"{NATICK}/CARD2/frame.png", "image"),
    ]  # nested folders walked; .txt/.md and the NAS's @eaDir thumbnails left out
    first = rows[3]
    assert set(first) == {"root", "rel", "size_bytes", "mtime", "media"}
    assert first["root"] == "GUILD-RAW-Media" and first["size_bytes"] == 10
    assert first["mtime"].endswith("+00:00")
    assert note.startswith("Ultron finished: 7 media files (3 video, 4 image, 0.0 GB) in 4 folders")
    assert "(folder_inventory)" in note  # no model in the note


def test_missing_catalog_counts_every_file_unindexed(run, raw) -> None:
    _, note, _, side = run([str(raw)])
    assert "7 unindexed in 4 folders" in note
    assert f"No catalog at {raw / '_index' / 'catalog.jsonl'} yet" in note
    assert "Unindexed files: 7 of 7" in note
    assert f"- GUILD-RAW-Media/{NATICK}: 3/3" in note
    assert "- GUILD-RAW-Media: 1/1" in note  # files directly in the root
    assert side["unindexed"] == 7 and len(side["folders"]) == 4


def test_diff_against_catalog_per_folder(run, raw) -> None:
    natick = [
        {"root": "GUILD-RAW-Media", "relpath": f"{NATICK}/C001.MP4", "caption": "c", "tags": ["a"]},
        {"root": "GUILD-RAW-Media", "relpath": f"{NATICK}/C002.mov", "failed": "bad reply"},
        # an older absolute-path record still counts, and case doesn't matter on folding roots
        {"path": str(raw / NATICK / "still.jpg"), "caption": "c", "tags": ["b"]},
    ]
    _catalog(
        raw,
        [
            {
                "root": "GUILD-RAW-Media",
                "folder": NATICK,
                "index": f"{NATICK}/tags.jsonl",
                "files": 3,
                "tagged": 2,
                "skipped": 0,
                "failed": 1,
                "model": "m",
                "updated_at": "2026-11-22T00:00:00+00:00",
            },
            {"root": "GUILD-RAW-Media", "folder": "Dump", "index": "Dump/tags.jsonl"},
            {"root": "OTHER-ROOT", "folder": "x", "index": "x/tags.jsonl"},
        ],  # fmt: skip
        {f"{NATICK}/tags.jsonl": natick},
    )
    _, note, rows, side = run([str(raw)])
    assert len(rows) == 7
    assert "4 unindexed in 3 folders" in note
    assert "Unindexed files: 4 of 7" in note
    assert "1 indexed file(s) have only a failed/skipped record" in note
    assert "1 folder index(es) read" in note
    assert "1 listed index(es) unreadable" in note and "Dump/tags.jsonl" in note
    assert f"GUILD-RAW-Media/{NATICK}:" not in note  # fully indexed: not listed
    for line in (f"- GUILD-RAW-Media/{NATICK}/CARD2: 1/1", "- GUILD-RAW-Media/Dump: 2/2"):
        assert line in note
    assert {(f["folder"], f["unindexed"], f["files"]) for f in side["folders"]} == {
        (f"{NATICK}/CARD2", 1, 1),
        ("Dump", 2, 2),
        ("", 1, 1),
    }


def test_group_depth_rolls_counts_up(run, raw) -> None:
    _, note, _, side = run([str(raw)], group_depth=1)
    assert {(f["folder"], f["unindexed"]) for f in side["folders"]} == {
        ("Guild Hall", 4),
        ("Dump", 2),
        ("", 1),
    }


def test_root_relative_inputs_resolve_through_media_roots(run, raw, tmp_path) -> None:
    _, note, rows, _ = run(["GUILD-RAW-Media/Guild Hall", f"GUILD-RAW-Media/{NATICK}/CARD2"])
    assert [r["rel"] for r in rows] == [
        f"{NATICK}/C001.MP4",
        f"{NATICK}/C002.mov",
        f"{NATICK}/still.jpg",
        f"{NATICK}/CARD2/frame.png",
    ]  # overlapping inputs: each file once
    assert note.startswith("Ultron finished: 4 media files")


def test_inputs_must_be_allowed_folders(run, raw, tmp_path) -> None:
    _, failed, _, _ = run(["GUILD-RAW-Media/../../../.."], status="needs_david")
    assert "outside runner.allowed_roots" in failed[0]["data"]["error"]


def test_input_file_is_refused(run, raw) -> None:
    _, failed, _, _ = run([str(raw / "top.jpg")], status="needs_david")
    assert "is not a folder" in failed[0]["data"]["error"]


def test_model_kinds_still_need_a_model(make, library, tmp_path) -> None:
    runner, bb, _ = make()
    bb.add("t1", model=None, inputs=[str(library)], output=str(tmp_path / "o.jsonl"))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    [failed] = [e for e in bb.events if e["action"] == "ultron.failed"]
    assert "missing model" in failed["data"]["error"]


def test_long_folder_lists_are_cut_to_the_note_budget() -> None:
    lines = [f"- R/folder {i:03d}: 1/1" for i in range(500)]
    kept = _fit(lines, 1000, Path("/x/inv.unindexed.json"))
    assert sum(len(k) + 1 for k in kept) <= 1000
    assert (
        kept[-1]
        == f"- ... {500 - len(kept) + 1} more folder(s): full list in /x/inv.unindexed.json"
    )
    assert _fit(lines[:3], 1000, Path("/x")) == lines[:3]
