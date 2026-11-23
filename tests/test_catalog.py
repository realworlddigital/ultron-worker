"""The catalog layer: tag records carry size + mtime, a job seeds its 'already done' set from its
existing output index (cards are disposable), every completion upserts the media root's TOC and
rebuilds the merged all-tags.jsonl, and startup sweeps frame scratch left by a hard kill."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ultron_worker import __main__ as cli
from ultron_worker.catalog import ALL_TAGS_NAME, CATALOG_DIR, TOC_NAME, update_catalog
from ultron_worker.media import MediaRoots

from .conftest import PNG


def rows(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def tag(runner, bb, tid: str, library: Path, **params) -> None:
    bb.add(tid, inputs=[str(library)], output=str(library / "tags.jsonl"), params=params)
    runner.tick()
    assert bb.tasks[tid]["status"] == "done", bb.notes


def note(bb, tid: str) -> str:
    return next(n["body"] for t, n in bb.notes if t == tid)


# ---- stamps + index-seeded skipping ------------------------------------------
def test_records_carry_size_and_mtime(make, library) -> None:
    runner, bb, _ = make()
    tag(runner, bb, "t1", library)
    for r in rows(library / "tags.jsonl"):
        st = os.stat(r["path"])
        assert r["size_bytes"] == st.st_size == len(PNG)
        assert abs(r["mtime"] - st.st_mtime) < 0.01


def test_a_new_card_over_an_unchanged_folder_makes_no_model_calls(make, library) -> None:
    runner, bb, ollama = make()
    tag(runner, bb, "t1", library)
    first = rows(library / "tags.jsonl")
    ollama.calls.clear()
    tag(runner, bb, "t2", library)  # e.g. a superseding card: no checkpoint of its own
    assert ollama.calls == []
    assert rows(library / "tags.jsonl") == first
    assert "5 unchanged since the existing index" in note(bb, "t2")
    progress = [
        e for e in bb.events if e["action"].endswith(".progress") and e["target"]["id"] == "t2"
    ]
    assert progress[0]["data"]["done"] == 5  # counted as done from the start


def test_changed_and_new_files_are_tagged_the_rest_skipped(make, library) -> None:
    runner, bb, ollama = make()
    tag(runner, bb, "t1", library)
    ollama.calls.clear()
    st = os.stat(library / "b.png")
    os.utime(library / "b.png", (st.st_atime, st.st_mtime + 100))
    (library / "sub" / "f.png").write_bytes(PNG)
    tag(runner, bb, "t2", library)
    assert sorted(Path(c["images"][0]).name for c in ollama.calls) == ["b.png", "f.png"]
    by_name = {Path(r["path"]).name: r for r in rows(library / "tags.jsonl")}
    assert len(by_name) == 6
    assert abs(by_name["b.png"]["mtime"] - (st.st_mtime + 100)) < 0.01
    assert "4 unchanged" in note(bb, "t2")


def test_changed_size_re_tags(make, library) -> None:
    runner, bb, ollama = make()
    tag(runner, bb, "t1", library)
    ollama.calls.clear()
    st = os.stat(library / "c.png")
    (library / "c.png").write_bytes(PNG + b"\0")
    os.utime(library / "c.png", (st.st_atime, st.st_mtime))  # same mtime, new size
    tag(runner, bb, "t2", library)
    assert [Path(c["images"][0]).name for c in ollama.calls] == ["c.png"]


def test_legacy_records_are_trusted_once_and_backfilled(make, library) -> None:
    runner, bb, ollama = make()
    tag(runner, bb, "t1", library)
    legacy = [
        {k: v for k, v in r.items() if k not in ("size_bytes", "mtime")}
        for r in rows(library / "tags.jsonl")
    ]
    (library / "tags.jsonl").write_text("".join(json.dumps(r) + "\n" for r in legacy))
    ollama.calls.clear()
    tag(runner, bb, "t2", library)
    assert ollama.calls == []
    for r in rows(library / "tags.jsonl"):
        assert r["size_bytes"] == len(PNG)
        assert abs(r["mtime"] - os.stat(r["path"]).st_mtime) < 0.01


def test_only_well_formed_tagged_records_seed(make, library) -> None:
    runner, bb, ollama = make()
    tag(runner, bb, "t1", library)
    records = rows(library / "tags.jsonl")
    records[0] = {k: records[0][k] for k in ("root", "relpath", "path")} | {"failed": "bad"}
    records[1]["tags"] = []
    (library / "tags.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    ollama.calls.clear()
    tag(runner, bb, "t2", library)
    assert len(ollama.calls) == 2


def test_unreadable_or_ignored_index_seeds_nothing(make, library) -> None:
    runner, bb, ollama = make()
    tag(runner, bb, "t1", library)
    ollama.calls.clear()
    tag(runner, bb, "t2", library, ignore_index=True)
    assert len(ollama.calls) == 5
    (library / "tags.jsonl").write_text("not json\n")
    ollama.calls.clear()
    tag(runner, bb, "t3", library)
    assert len(ollama.calls) == 5


# ---- TOC + merged all-tags ---------------------------------------------------
def test_completion_upserts_the_toc_and_rebuilds_all_tags(make, library, tmp_path) -> None:
    root = tmp_path.name  # cfg's allowed root is tmp_path, so that is the media root
    other = tmp_path / "Camps"
    other.mkdir()
    (other / "x.png").write_bytes(PNG)
    (library / "x.png").write_bytes(PNG)
    runner, bb, _ = make()
    tag(runner, bb, "t1", library)
    toc_path = tmp_path / CATALOG_DIR / TOC_NAME
    (entry,) = rows(toc_path)
    assert entry["root"] == root and entry["folder"] == "library"
    assert entry["index"] == "library/tags.jsonl"
    assert (entry["files"], entry["tagged"], entry["skipped"], entry["failed"]) == (6, 6, 0, 0)
    assert entry["model"] == "llava:13b" and entry["updated_at"].endswith("+00:00")
    assert f"catalog {root}/{CATALOG_DIR} updated (1 folder(s))" in note(bb, "t1")

    tag(runner, bb, "t2", other)
    tag(runner, bb, "t3", library)  # same folder again: upserted in place, not appended
    toc = rows(toc_path)
    assert [e["folder"] for e in toc] == ["library", "Camps"]
    assert toc[0]["updated_at"] >= entry["updated_at"]
    merged = rows(tmp_path / CATALOG_DIR / ALL_TAGS_NAME)
    assert [r["relpath"] for r in merged] == [
        "library/a.png",
        "library/b.png",
        "library/c.png",
        "library/sub/d.png",
        "library/sub/e.png",
        "library/x.png",
        "Camps/x.png",
    ]
    assert all(r["root"] == root and r["tags"] for r in merged)


def test_folder_falls_back_to_the_index_folder(make, library, tmp_path) -> None:
    runner, bb, _ = make()
    bb.add(
        "t1",
        inputs=[str(library / "a.png"), str(library / "b.png")],
        output=str(tmp_path / "out" / "pair.json"),
    )
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done", bb.notes
    (entry,) = rows(tmp_path / CATALOG_DIR / TOC_NAME)
    assert (entry["folder"], entry["index"]) == ("out", "out/pair.json")
    assert entry["tagged"] == 2


def test_a_missing_index_is_skipped_and_named(make, library, tmp_path) -> None:
    other = tmp_path / "Camps"
    other.mkdir()
    (other / "x.png").write_bytes(PNG)
    runner, bb, _ = make()
    tag(runner, bb, "t1", other)
    (other / "tags.jsonl").unlink()
    tag(runner, bb, "t2", library)
    body = note(bb, "t2")
    assert f"skipped missing/unreadable index(es): {tmp_path.name}/Camps/tags.jsonl" in body
    assert len(rows(tmp_path / CATALOG_DIR / TOC_NAME)) == 2  # the entry stays listed
    merged = rows(tmp_path / CATALOG_DIR / ALL_TAGS_NAME)
    assert [r["relpath"].split("/")[0] for r in merged] == ["library"] * 5


def test_merge_semantics_and_unreadable_index(tmp_path) -> None:
    base = tmp_path / "Media"
    for d in ("A", "B", "C"):
        (base / d).mkdir(parents=True)
    roots = MediaRoots.build(None, [base])
    rec = {"root": "Media", "relpath": "A/x.png"}
    (base / "A" / "tags.jsonl").write_text(
        json.dumps({**rec, "caption": "c", "tags": ["x"]}) + "\n"
    )
    (base / "B" / "tags.jsonl").write_text(json.dumps({**rec, "failed": "bad"}) + "\n")
    (base / "C" / "tags.jsonl").write_text("{broken\n")
    common = {"counts": {"files": 1, "tagged": 1, "skipped": 0, "failed": 0}, "model": "m"}
    for d in ("A", "C", "B"):
        msg = update_catalog(
            roots,
            Path,
            index_path=base / d / "tags.jsonl",
            folder_path=base / d,
            **common,
        )
    assert "skipped missing/unreadable index(es): Media/C/tags.jsonl" in msg
    assert [e["folder"] for e in rows(base / CATALOG_DIR / TOC_NAME)] == ["A", "C", "B"]
    (merged,) = rows(base / CATALOG_DIR / ALL_TAGS_NAME)
    assert merged["tags"] == ["x"]  # tagged beats a later failed record

    (base / CATALOG_DIR / TOC_NAME).write_text("garbage\n")
    msg = update_catalog(
        roots, Path, index_path=base / "A" / "tags.jsonl", folder_path=base / "A", **common
    )
    assert msg.startswith("catalog not updated") and "catalog.jsonl" in msg
    assert (base / CATALOG_DIR / TOC_NAME).read_text() == "garbage\n"  # never clobbered


def test_output_outside_every_media_root_still_finishes(make, cfg, library, tmp_path) -> None:
    import dataclasses

    c = dataclasses.replace(cfg, allowed_roots=(Path("/"),))  # a bare root has no root name
    runner, bb, ollama = make(config=c)
    tag(runner, bb, "t1", library)
    assert "catalog not updated: the output is outside every media root" in note(bb, "t1")
    assert not (tmp_path / CATALOG_DIR).exists()
    ollama.calls.clear()
    tag(runner, bb, "t2", library)  # seeding falls back to the absolute path identity
    assert ollama.calls == []


# ---- scratch sweep -----------------------------------------------------------
def test_startup_sweeps_frame_scratch_left_by_a_hard_kill(make, cfg, library) -> None:
    jobs_dir = cfg.state_dir / "jobs"
    stale = jobs_dir / "t-old" / "frames" / "0123456789abcdef"
    stale.mkdir(parents=True)
    (stale / "frame00.jpg").write_bytes(b"x")
    keep = jobs_dir / "t-old" / "notes.txt"
    keep.write_text("not scratch")
    runner, bb, _ = make()
    assert runner.sweep_scratch() == 1
    assert not (jobs_dir / "t-old" / "frames").exists() and keep.is_file()
    assert runner.sweep_scratch() == 0
    shutil.rmtree(jobs_dir)
    assert runner.sweep_scratch() == 0  # no jobs dir yet: nothing to do


def test_the_cli_sweeps_before_running_but_check_does_not(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    class Fake:
        def __init__(self, cfg, background: bool = True) -> None:  # type: ignore[no-untyped-def]
            pass

        def sweep_scratch(self) -> int:
            calls.append("sweep")
            return 0

        def tick(self) -> None:
            calls.append("tick")

    from .test_log_and_lock import write_config

    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "test-key")
    monkeypatch.setattr(cli, "Runner", Fake)
    monkeypatch.setattr(cli, "_check", lambda runner: 0)
    cfg_path = write_config(tmp_path, tmp_path / "state")
    assert cli.main(["--config", str(cfg_path), "--once"]) == 0
    assert calls == ["sweep", "tick"]
    calls.clear()
    assert cli.main(["--config", str(cfg_path), "--check"]) == 0
    assert calls == []


def test_seeded_items_keep_failing_new_files_from_looking_like_a_broken_job(
    make, library, monkeypatch
) -> None:
    runner, bb, _ = make()
    tag(runner, bb, "t1", library)
    for i in range(6):
        (library / f"v{i}.mp4").write_bytes(b"not a video")
    monkeypatch.setattr("ultron_worker.jobs.vision.shutil.which", lambda name: None)
    tag(runner, bb, "t2", library)  # no ffmpeg: 6 videos skipped, 5 photos seeded
    assert "5 tagged, 6 skipped" in note(bb, "t2")
