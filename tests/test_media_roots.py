"""Tag records carry a media-root name + relative path, so the library index survives a NAS swap:
an index written against one base path resolves against another, older absolute-path indexes are
normalized on read, and passes merge on root + relpath."""

from __future__ import annotations

import dataclasses
import json
import shutil
import threading
from pathlib import Path

import pytest

from ultron_worker.config import ConfigError, load
from ultron_worker.jobs.base import JobContext, JobError
from ultron_worker.media import MediaRoots, load_index

from .conftest import PNG

NAS = "\\\\GUILD-NAS\\GUILD-RAW-Media"
# The shape of the two September outputs: absolute UNC paths, no root/relpath.
SEPTEMBER = [
    {
        "path": NAS + "\\Guild Hall\\September\\C2706.MP4",
        "media": "video",
        "caption": "A coach at a whiteboard.",
        "tags": ["whiteboard"],
        "frames": 4,
    },
    {"path": NAS + "\\Guild Hall\\September\\C2707.MP4", "media": "video", "failed": "bad reply"},
    {"path": "D:\\Elsewhere\\x.jpg", "media": "image", "caption": "A dock.", "tags": ["dock"]},
]


def test_roots_derive_from_allowed_roots_by_final_component() -> None:
    roots = MediaRoots.build(None, [Path(NAS), Path("D:\\Photos"), Path("D:\\"), "/srv/Videos/"])
    assert roots.roots == {"GUILD-RAW-Media": NAS, "Photos": "D:\\Photos", "Videos": "/srv/Videos/"}


def test_explicit_table_wins_and_ambiguous_derived_names_are_left_out() -> None:
    roots = MediaRoots.build({"Raw": "\\\\BIG-NAS\\GUILD-RAW-Media"}, ["/a/Photos", "/b/Photos"])
    assert roots.roots == {"Raw": "\\\\BIG-NAS\\GUILD-RAW-Media"}
    with pytest.raises(ValueError, match="plain folder-style name"):
        MediaRoots({"a/b": "/x"})


def test_config_without_media_roots_table_derives_them(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ULTRON_BACKBONE_KEY", "k")
    body = (
        '[backbone]\nurl = "https://bb.example"\n[runner]\n'
        "allowed_roots = ['\\\\GUILD-NAS\\GUILD-RAW-Media', 'D:\\Photos']\n"
    )
    p = tmp_path / "config.toml"
    p.write_text(body)
    cfg = load(p)
    assert cfg.media_roots == ()
    assert cfg.roots().roots == {"GUILD-RAW-Media": NAS, "Photos": "D:\\Photos"}
    p.write_text(body + '[media_roots]\nGUILD-RAW-Media = "\\\\\\\\BIG-NAS\\\\Raw"\n')
    assert load(p).roots().roots == {"GUILD-RAW-Media": "\\\\BIG-NAS\\Raw", "Photos": "D:\\Photos"}
    p.write_text(body + '[media_roots]\n"bad/name" = "/x"\n')
    with pytest.raises(ConfigError, match="plain folder-style name"):
        load(p)


def test_september_absolute_records_normalize_to_root_and_relpath() -> None:
    roots = MediaRoots.build(None, [Path(NAS)])
    first, failed, outside = (roots.normalize(r) for r in SEPTEMBER)
    assert first["root"] == "GUILD-RAW-Media"
    assert first["relpath"] == "Guild Hall/September/C2706.MP4"
    assert first["path"] == SEPTEMBER[0]["path"] and first["tags"] == ["whiteboard"]
    assert list(first)[:2] == ["root", "relpath"]
    assert (failed["root"], failed["relpath"]) == (
        "GUILD-RAW-Media",
        "Guild Hall/September/C2707.MP4",
    )
    assert outside == SEPTEMBER[2]  # no known root: left exactly as it was
    # UNC paths are case-insensitive: a differently-cased prefix is the same root.
    lower = roots.normalize({"path": NAS.lower() + "\\Guild Hall\\September\\C2706.MP4"})
    assert roots.identity(lower) == roots.identity(first)


def test_september_index_resolves_against_a_swapped_nas(tmp_path) -> None:
    index = tmp_path / "2026-09-tags.jsonl"
    index.write_text("".join(json.dumps(r) + "\n" for r in SEPTEMBER))
    bigger = MediaRoots({"GUILD-RAW-Media": "\\\\GUILD-NAS2\\Media\\GUILD-RAW-Media"})
    first = load_index(index, bigger)[0]
    assert "root" not in first  # the old prefix isn't a known root any more...
    old = MediaRoots.build(None, [Path(NAS)])
    rec = load_index(index, old)[0]  # ...so normalize while the old prefix is configured,
    assert bigger.absolute(rec) == (  # and the stored root+relpath then follows the swap
        "\\\\GUILD-NAS2\\Media\\GUILD-RAW-Media\\Guild Hall\\September\\C2706.MP4"
    )
    on_linux_box = MediaRoots({"GUILD-RAW-Media": "/mnt/nas/GUILD-RAW-Media"})
    assert on_linux_box.absolute(rec) == "/mnt/nas/GUILD-RAW-Media/Guild Hall/September/C2706.MP4"


def test_merge_keys_on_root_and_relpath_across_passes() -> None:
    roots = MediaRoots.build(None, [Path(NAS)])
    later = [
        {  # same file as SEPTEMBER[0], written by the new runner: replaces it
            "root": "GUILD-RAW-Media",
            "relpath": "Guild Hall/September/C2706.MP4",
            "path": "\\\\NEW-NAS\\GUILD-RAW-Media\\Guild Hall\\September\\C2706.MP4",
            "caption": "A coach writes on a whiteboard.",
            "tags": ["whiteboard", "marker"],
        },
        {  # a later failure never replaces a good tag record...
            "root": "GUILD-RAW-Media",
            "relpath": "guild hall/september/c2706.mp4",
            "failed": "bad reply",
        },
        {  # ...but a later tag record replaces an earlier failure
            "root": "GUILD-RAW-Media",
            "relpath": "Guild Hall/September/C2707.MP4",
            "caption": "Kids at a camp.",
            "tags": ["camp"],
        },
    ]
    merged = roots.merge(SEPTEMBER, SEPTEMBER, later)
    assert len(merged) == 3
    by_rel = {r.get("relpath", r.get("path")): r for r in merged}
    assert by_rel["Guild Hall/September/C2706.MP4"]["tags"] == ["whiteboard", "marker"]
    assert by_rel["Guild Hall/September/C2707.MP4"]["tags"] == ["camp"]
    assert "D:\\Elsewhere\\x.jpg" in by_rel


def _library(base: Path) -> None:
    for name in ("Guild Hall/September/a.png", "Guild Hall/September/b.png", "Camps/c.png"):
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_bytes(PNG)


def test_nas_swap_index_written_on_a_resolves_on_b(make, cfg, tmp_path) -> None:
    base_a = tmp_path / "nas-a" / "GUILD-RAW-Media"
    base_b = tmp_path / "nas-b" / "bigger" / "GUILD-RAW-Media"
    _library(base_a)
    on_a = dataclasses.replace(cfg, allowed_roots=(base_a,))
    runner, bb, _ = make(config=on_a)
    # Inputs and output in the ROOT/relative form, resolved through the derived mapping.
    bb.add("t1", inputs=["GUILD-RAW-Media/Guild Hall"], output="GUILD-RAW-Media/index/tags.jsonl")
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    index_a = base_a / "index" / "tags.jsonl"
    rows = [json.loads(x) for x in index_a.read_text().splitlines()]
    assert [(r["root"], r["relpath"]) for r in rows] == [
        ("GUILD-RAW-Media", "Guild Hall/September/a.png"),
        ("GUILD-RAW-Media", "Guild Hall/September/b.png"),
    ]

    # The NAS is swapped: everything moves to a different base path, the old one is gone.
    shutil.copytree(base_a, base_b)
    shutil.rmtree(tmp_path / "nas-a")
    roots_b = MediaRoots.build(None, [base_b])
    found = load_index(base_b / "index" / "tags.jsonl", roots_b)
    assert [roots_b.identity(r) for r in found] == [roots_b.identity(r) for r in rows]
    for rec in found:
        assert Path(roots_b.absolute(rec)) == base_b / rec["relpath"]
        assert Path(roots_b.absolute(rec)).is_file()

    # A later pass on B merges the earlier index without duplicating its files.
    on_b = dataclasses.replace(cfg, allowed_roots=(base_b,))
    runner_b, bb_b, ollama_b = make(config=on_b)
    bb_b.add(
        "t2",
        inputs=["GUILD-RAW-Media/Camps"],
        output="GUILD-RAW-Media/index/all.json",
        params={"merge": ["GUILD-RAW-Media/index/tags.jsonl"]},
    )
    runner_b.tick()
    assert bb_b.tasks["t2"]["status"] == "done", bb_b.notes
    doc = json.loads((base_b / "index" / "all.json").read_text())
    assert doc["tagged"] == 3 and doc["media_roots"] == ["GUILD-RAW-Media"]
    assert sorted(r["relpath"] for r in doc["items"]) == [
        "Camps/c.png",
        "Guild Hall/September/a.png",
        "Guild Hall/September/b.png",
    ]
    assert len(ollama_b.calls) == 1
    assert "merged with 1 earlier index" in bb_b.notes[0][1]["body"]


def _ctx(tmp_path: Path, root: Path) -> JobContext:
    return JobContext(
        task_id="t", ollama=None, allowed_roots=(root,), work_dir=tmp_path / "w",
        stop=threading.Event(),
    )  # fmt: skip


def test_job_paths_accept_absolute_and_root_relative_forms(tmp_path) -> None:
    base = tmp_path / "GUILD-RAW-Media"
    _library(base)
    ctx = _ctx(tmp_path, base)
    want = (base / "Camps" / "c.png").resolve()
    assert ctx.allowed(str(base / "Camps" / "c.png"), what="input") == want
    assert ctx.allowed("GUILD-RAW-Media/Camps/c.png", what="input") == want
    assert ctx.allowed("GUILD-RAW-Media\\Camps\\c.png", what="input") == want
    assert ctx.allowed("GUILD-RAW-Media", what="input") == base.resolve()
    # allowed_roots is checked on the resolved path, so a root ref can't climb out.
    with pytest.raises(JobError, match="outside runner.allowed_roots"):
        ctx.allowed("GUILD-RAW-Media/../../etc/passwd", what="input")
    with pytest.raises(JobError, match="outside runner.allowed_roots"):
        ctx.allowed("Unknown-Root/x.png", what="input")


def test_bad_merge_param_goes_to_needs_david(make, library, tmp_path) -> None:
    runner, bb, _ = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "t.jsonl"), params={"merge": 3})
    bb.add("t2", inputs=[str(library)], output=str(tmp_path / "u.jsonl"),
           params={"merge": [str(tmp_path / "missing.jsonl")]})  # fmt: skip
    runner.tick()
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    assert bb.tasks["t2"]["status"] == "needs_david"


def test_root_names_differing_only_in_case_are_not_merged() -> None:
    roots = MediaRoots.build(None, ["D:\\Photos", "E:\\photos", "/srv/Videos"])
    assert roots.roots == {"Videos": "/srv/Videos"}  # ambiguous derived names left out
    with pytest.raises(ValueError, match="differ only in case"):
        MediaRoots({"Photos": "D:\\Photos", "photos": "E:\\photos"})


def test_case_sensitive_roots_keep_case_variant_files_apart() -> None:
    # A Windows mapping next to a POSIX root: only the Windows root compares without case.
    roots = MediaRoots({"Raw": NAS, "Local": "/srv/Local"})
    local = [
        {"path": "/srv/Local/A.jpg", "tags": ["a"]},
        {"path": "/srv/Local/a.jpg", "tags": ["b"]},
    ]
    assert len(roots.merge(local)) == 2
    nas = [
        {"path": NAS + "\\A.jpg", "tags": ["a"]},
        {"path": NAS.lower() + "\\a.jpg", "tags": ["b"]},
    ]
    assert [r["tags"] for r in roots.merge(nas)] == [["b"]]
    assert roots.relativize("/srv/local/A.jpg") is None  # POSIX prefix match keeps case
