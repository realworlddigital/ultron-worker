"""clip_segment: long videos cut into virtual, range-tagged clips (record.media_asset clips)."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ultron_worker.backbone import Backbone, ScopeViolation
from ultron_worker.jobs import segment
from ultron_worker.jobs.publish import asset_id
from ultron_worker.jobs.segment import parse_analysis, parse_verdict, propose
from ultron_worker.jobs.vision import TagParseError

from .conftest import FakeOllama

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg"
)
ROOT = "GUILD-RAW-Media"
NATICK = "Guild Hall/2026-10-GH NATICK"
OTHER = "Unsorted/2026-11 dump"
WINDOW = {"min_sec": 12.0, "max_sec": 40.0, "target_min_sec": 15.0, "target_max_sec": 30.0}


def _ffmpeg(out: Path, seconds: int, *, silences=(), colors=None) -> None:
    if colors:  # one solid colour per stretch: hard scene changes, no audio
        n = len(colors)
        inputs = [
            a
            for c in colors
            for a in ("-f", "lavfi", "-i", f"color=c={c}:s=160x120:r=5:d={seconds / n:g}")
        ]
        graph = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
        cmd = [*inputs, "-filter_complex", graph, "-map", "[v]"]
    else:  # a tone with silent stretches: speech breaks
        mute = "+".join(f"between(t,{a},{b})" for a, b in silences) or "0"
        cmd = [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s=160x120:r=5:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=f=440:d={seconds}",
            "-af",
            f"volume=enable='{mute}':volume=0",
            "-c:a",
            "aac",
        ]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *cmd, "-c:v", "libx264", "-preset",
                    "ultrafast", str(out)], check=True)  # fmt: skip


@pytest.fixture(scope="session")
def videos(tmp_path_factory) -> dict[str, Path]:
    base = tmp_path_factory.mktemp("videos")
    made = {
        "talk.mp4": dict(seconds=95, silences=[(24, 26), (50, 53), (85, 95)]),
        "build.mp4": dict(seconds=70, colors=["red", "green", "blue", "white"]),
        "short.mp4": dict(seconds=20, silences=[]),
        "other.mp4": dict(seconds=60, silences=[(30, 31)]),
    }
    for name, kw in made.items():
        _ffmpeg(base / name, **kw)
    return {name: base / name for name in made}


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def raw(tmp_path: Path, videos) -> Path:
    """NATICK: two long presenter takes and a short one;
    an unmapped dump folder with a long take. All published as parents, as catalog_publish
    leaves them."""
    base = tmp_path / "nas" / ROOT
    toc = []
    for folder, files in ((NATICK, ["talk.mp4", "build.mp4", "short.mp4"]), (OTHER, ["other.mp4"])):
        (base / folder).mkdir(parents=True)
        recs = []
        for name in files:
            dest = base / folder / (name if folder == NATICK else "take.mp4")
            shutil.copyfile(videos[name], dest)
            recs.append({"root": ROOT, "relpath": f"{folder}/{dest.name}", "media": "video",
                         "caption": f"whole take {dest.name}", "tags": ["lecture"],
                         "content_hash": _sha(dest)})  # fmt: skip
        (base / folder / "tags.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs))
        toc.append({"root": ROOT, "folder": folder, "index": f"{folder}/tags.jsonl"})
    (base / "_index").mkdir()
    (base / "_index" / "catalog.jsonl").write_text("".join(json.dumps(e) + "\n" for e in toc))
    return base


def _parents(raw: Path) -> dict[str, dict]:
    out = {}
    for folder, org in ((NATICK, "guild_hall"), (OTHER, "gcs")):
        for line in (raw / folder / "tags.jsonl").read_text().splitlines():
            rec = json.loads(line)
            duration = float(
                subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", str(raw / rec["relpath"])],
                               capture_output=True, text=True, check=True).stdout
            )  # fmt: skip
            technical = {"width": 160, "height": 120, "codec": "h264", "fps": 5.0,
                         "duration_sec": round(duration, 3)}  # fmt: skip
            aid = asset_id(rec["content_hash"])
            out[aid] = {"id": aid, "type": "record.media_asset", "version": 1, "data": {
                "content_hash": rec["content_hash"], "kind": "video", "org": org,
                "rights": "needs_check", "technical": technical,
                "tags": {"structured": {"caption": rec["caption"]}, "flat": ["lecture"]},
                "provenance": {"feeder": "nas_mirror", "ingested_by": "ultron-runner",
                               "ingested_at": "2026-11-25T00:00:00+00:00", "source_ref": "t0"},
                "locations": [{"storage_id": "nas-main", "path": f"{ROOT}/{rec['relpath']}",
                               "status": "verified", "last_verified_at": None}],
            }}  # fmt: skip
    return out


RANGE = re.compile(r"\(([0-9.]+)s to ([0-9.]+)s")


class Glimmer(FakeOllama):
    """Judges each segment from its range in the prompt: the segment starting in 25-35 s is
    'setup' (rejected); every other one is kept with a caption and tags naming its range."""

    def generate(self, model, prompt, *, images=None, json_format=False, options=None):
        with self.lock:
            self.calls.append({"model": model, "images": [str(i) for i in images or []],
                               "prompt": prompt})  # fmt: skip
            n = len(self.calls)
        if self.hook:
            self.hook(n, self.calls[-1])
        start, end = (float(x) for x in RANGE.search(prompt).groups())
        if 25 <= start < 35:
            return json.dumps({"usable": False, "reason": "setup, nobody talking"})
        return json.dumps({"usable": True, "reason": "presenter mid-sentence",
                           "caption": f"presenter explains the build at {start:.0f}s",
                           "tags": [f"part {start:.0f}", "whiteboard", "man"]})  # fmt: skip


def _item_dir(call: dict) -> str:
    return str(Path(call["images"][0]).parent.parent)


@pytest.fixture
def run(make, cfg, raw, tmp_path):
    """run(tid, status="done", inputs=None, **params) -> (bb, note, report, ollama)."""
    config = replace(cfg, media_roots=((ROOT, str(raw)),))
    state: dict = {}

    def _run(tid="t1", *, status="done", inputs=None, add=True, ticks=1, hook=None, **params):
        ollama = state.get("ollama") or Glimmer()
        ollama.hook = hook
        runner, bb, _ = make(bb=state.get("bb"), ollama=ollama, config=config)
        if "bb" not in state:
            bb.assets.update(_parents(raw))
        state.update(bb=bb, ollama=ollama, runner=runner)
        out = tmp_path / "out" / f"{tid}.json"
        if add:
            inputs = inputs or [f"{ROOT}/{NATICK}"]
            bb.add(tid, kind="clip_segment", model="muse-glimmer:30b", inputs=inputs,
                   output=str(out), params=params)  # fmt: skip
        if hook:
            hook.runner = runner
        for _ in range(ticks):
            runner.tick()
        assert bb.tasks[tid]["status"] == status, bb.events
        notes = [n["body"] for t, n in bb.notes if t == tid and n["body"].startswith("Ultron fin")]
        report = json.loads(out.read_text()) if status == "done" else None
        return bb, (notes[-1] if notes else None), report, ollama

    return _run


ORGS = {f"{ROOT}/Guild Hall": "guild_hall"}


def _clips(bb) -> dict[str, dict]:
    return {k: v["data"] for k, v in bb.assets.items() if "parent_asset_id" in v["data"]}


def test_long_takes_become_range_tagged_clips(run, raw) -> None:
    bb, note, report, ollama = run(orgs=ORGS)
    parents = {a: d["data"] for a, d in bb.assets.items() if "content_hash" in d["data"]}
    talk = next(a for a, d in parents.items() if d["locations"][0]["path"].endswith("talk.mp4")
                and NATICK in d["locations"][0]["path"])  # fmt: skip
    clips = _clips(bb)
    assert clips and {c["parent_asset_id"] for c in clips.values()} == {
        a for a, d in parents.items() if d["org"] == "guild_hall" and "short" not in json.dumps(d)
    }
    for cid, c in clips.items():
        start, end = c["range_start_sec"], c["range_end_sec"]
        assert 12 <= end - start <= 40 and c["technical"]["duration_sec"] == round(end - start, 3)
        assert c["technical"]["width"] == 160  # derived from the parent by the backbone
        assert c["org"] == "guild_hall" and c["rights"] is None and c["kind"] == "video"
        assert "content_hash" not in c and "locations" not in c
        # tags describe THIS range, never the parent take's caption
        assert c["tags"]["structured"]["caption"] == f"presenter explains the build at {start:.0f}s"
        assert c["tags"]["flat"] == [f"part {start:.0f}", "whiteboard"]  # filler dropped
        assert "whole take" not in json.dumps(c)
        assert c["provenance"]["feeder"] == "ultron_segmentation"
        assert c["provenance"]["source_ref"] == "t1"
        assert c["tags"]["structured"]["segmented_by"] == "muse-glimmer:30b"
        assert cid in note
    # the talk's dead air sits between clips, and the trailing silence is in none
    talk_clips = sorted((c["range_start_sec"], c["range_end_sec"]) for c in clips.values()
                        if c["parent_asset_id"] == talk)  # fmt: skip
    assert talk_clips[0] == (0.0, pytest.approx(24.2, abs=0.1))
    assert all(e <= 85.3 for _, e in talk_clips)
    t = report["totals"]
    assert t["assets"] == 3 and t["short"] == 1 and t["held"] == 0 and t["failed"] == 0
    assert t["kept"] == len(clips) == t["created"] and t["proposed"] == t["kept"] + 1  # 1 setup
    assert sorted(report["clip_ids_created"]) == sorted(clips)
    head = f"{t['proposed']} segments proposed, {t['kept']} kept, 0 held"
    assert note.startswith(f"Ultron finished: Segmented 3 asset(s) into virtual clips: {head}")
    assert f"Clip ids created: {len(clips)}:" in note
    assert all(c["model"] == "muse-glimmer:30b" for c in ollama.calls)
    assert all(len(c["images"]) == 3 for c in ollama.calls)


def test_rerun_upserts_and_never_duplicates(run) -> None:
    bb, _, first, _ = run(orgs=ORGS)
    before = json.loads(json.dumps(bb.assets))
    bb, note, report, _ = run("t2", orgs=ORGS)
    assert bb.assets.keys() == before.keys()
    assert report["totals"]["created"] == 0  # same ranges, same ids: upserts onto them
    assert report["totals"]["updated"] == first["totals"]["kept"]  # provenance names t2 now
    assert all(bb.assets[k]["version"] == 2 for k in _clips(bb))
    assert "Clip ids created" not in note


def test_unmapped_folder_is_held_and_named_never_published(run) -> None:
    bb, note, report, ollama = run(inputs=[f"{ROOT}/{NATICK}", f"{ROOT}/{OTHER}"], orgs=ORGS)
    held = [a for a in report["assets"] if a["outcome"] == "held"]
    assert [a["path"] for a in held] == [f"{ROOT}/{OTHER}/take.mp4"]
    assert report["totals"]["held"] == 1
    assert f"Held, not segmented (no backbone org): 1: {ROOT}/{OTHER}/take.mp4" in note
    assert held[0]["parent_asset_id"] not in {c["parent_asset_id"] for c in _clips(bb).values()}
    assert len({_item_dir(c) for c in ollama.calls}) == 2  # the held take never saw the model


def test_org_mapping_that_disagrees_with_the_parent_is_held(run) -> None:
    bb, note, report, _ = run(inputs=[f"{ROOT}/{OTHER}"], orgs={f"{ROOT}/Unsorted": "gea"})
    assert report["totals"]["held"] == 1 and not _clips(bb)
    assert "a clip always carries its parent's org" in note


def test_killed_and_resumed_job_does_not_resegment_finished_assets(run, monkeypatch) -> None:
    analysed: list[str] = []
    real = segment._analyse

    def spy(local, *a, **kw):
        analysed.append(str(local.parent))
        return real(local, *a, **kw)

    monkeypatch.setattr(segment, "_analyse", spy)

    def hook(n, call):
        dirs = hook.dirs
        dirs.setdefault(_item_dir(call), 0)
        dirs[_item_dir(call)] += 1
        if len(dirs) == 2 and dirs[_item_dir(call)] == 1:
            hook.runner.stop.set()  # killed while the second asset is part-judged

    hook.dirs = {}
    bb, _, _, ollama = run(status="in_progress", orgs=ORGS, hook=hook)
    first, second = list(hook.dirs)
    calls_before = len(ollama.calls)
    clips_before = len(_clips(bb))
    assert clips_before > 0  # the first asset's clips are published

    def count(n, call):
        count.dirs[_item_dir(call)] = count.dirs.get(_item_dir(call), 0) + 1

    count.dirs = {}
    bb, note, report, ollama = run(add=False, hook=count, orgs=ORGS)
    assert first not in count.dirs  # the finished asset: no model call
    assert analysed.count(first) == 1 and analysed.count(second) == 1  # analysed once each
    second_total = next(a["proposed"] for a in report["assets"] if a.get("proposed")
                        and a["path"].endswith("build.mp4"))  # fmt: skip
    assert count.dirs[second] == second_total - 1  # the segment judged before the kill isn't
    assert len(ollama.calls) - calls_before == second_total - 1
    assert report["totals"]["created"] == len(_clips(bb)) and report["totals"]["failed"] == 0


def test_asset_ids_as_inputs(run, raw) -> None:
    aid = next(a for a, d in _parents(raw).items() if NATICK in d["data"]["locations"][0]["path"]
               and d["data"]["locations"][0]["path"].endswith("talk.mp4"))  # fmt: skip
    bb, note, report, _ = run(inputs=[aid], orgs=ORGS)
    assert report["totals"]["assets"] == 1 and report["totals"]["kept"] > 0
    assert {c["parent_asset_id"] for c in _clips(bb).values()} == {aid}


def test_unpublished_parent_fails_its_asset(run, monkeypatch) -> None:
    from ultron_worker import runner

    monkeypatch.setattr(runner, "BROKEN_AFTER", 99)

    def prep(bb):
        for k in list(bb.assets):
            if bb.assets[k]["data"]["locations"][0]["path"].endswith("build.mp4"):
                del bb.assets[k]

    bb, note, report, _ = run(orgs=ORGS, ticks=0, status="dispatched")
    prep(bb)
    bb.tasks["t1"]["status"] = "dispatched"
    bb, note, report, _ = run(add=False, orgs=ORGS)
    assert report["totals"]["failed"] == 1
    assert "is not in the catalog: publish it first (catalog_publish)" in note


def test_outage_mid_publish_parks_then_resumes_without_duplicates(run) -> None:
    def hook(n, call):
        if n == 1:
            hook.runner.bb.asset_down_after = 2  # two clip writes, then down

    bb, _, _, _ = run(status="in_progress", orgs=ORGS, hook=hook)
    assert len(_clips(bb)) == 2
    bb.asset_down_after, bb.assets_down = None, False
    bb, _, report, _ = run(add=False, orgs=ORGS, ticks=3)
    assert report["totals"]["created"] == len(_clips(bb)) and report["totals"]["failed"] == 0


@pytest.mark.parametrize(
    ("params", "why"),
    [({"min_sec": 20, "target_min_sec": 15}, "min_sec <= target_min_sec"),
     ({"org": "Guild Hall"}, "params.org='Guild Hall'"),
     ({"frames": 0}, "params.frames")],
)  # fmt: skip
def test_bad_specs_go_to_david(run, params, why) -> None:
    bb, _, _, _ = run(status="needs_david", **params)
    [failed] = [e for e in bb.events if e["action"] == "ultron.failed"]
    assert why in failed["summary"]


# ---- units -------------------------------------------------------------------------
def test_propose_prefers_speech_breaks_and_stays_in_bounds() -> None:
    silences = [(24.0, 26.0), (50.0, 53.0), (80.0, 90.0)]
    assert propose(90.0, [40.0], silences, **WINDOW) == [
        (0.0, 24.2, "speech_break"), (25.8, 50.2, "speech_break"), (52.8, 80.2, "end")
    ]  # fmt: skip
    cuts = propose(1059.0, [], [], **WINDOW)
    assert cuts[0] == (0.0, 30.0, "forced") and cuts[-1][1] == 1059.0
    assert all(12 <= e - s <= 40 for s, e, _ in cuts)
    assert propose(1059.0, [], [], **WINDOW) == cuts  # deterministic: same ids on a re-run
    assert propose(50.0, [], [(0.0, 8.0)], **WINDOW)[0][0] == 7.8  # no clip opens on dead air
    assert propose(10.0, [], [], **WINDOW) == []
    assert propose(45.006, [], [], **WINDOW)[-1][1] == 45.0  # never past the parent's end


def test_parse_analysis_reads_scenes_and_silences() -> None:
    stderr = "\n".join([
        "[Parsed_showinfo_3 @ 0x1] n:   0 pts:  51 pts_time:10.2 duration:1",
        "[Parsed_showinfo_3 @ 0x1] n:   1 pts:  52 pts_time:10.4 duration:1",
        "[silencedetect @ 0x2] silence_start: 24.01",
        "[silencedetect @ 0x2] silence_end: 26.01 | silence_duration: 2",
        "[silencedetect @ 0x2] silence_start: 80.5",
    ])  # fmt: skip
    assert parse_analysis(stderr, 90.0) == ([10.2], [(24.01, 26.01), (80.5, 90.0)])


def test_parse_verdict_is_strict() -> None:
    assert parse_verdict('{"usable": false, "reason": "empty room"}', 10) == {
        "verdict": "rejected", "reason": "empty room"}  # fmt: skip
    kept = parse_verdict('{"usable": true, "caption": "a build", "tags": ["drill", "man"]}', 10)
    assert kept["verdict"] == "kept" and kept["tags"] == ["drill"]
    for bad in ('{"usable": "yes"}', '{"usable": true, "caption": "x", "tags": "a"}', "nope"):
        with pytest.raises(TagParseError):
            parse_verdict(bad, 10)


def test_client_scope_for_clips(cfg) -> None:
    bb = Backbone(cfg)
    parent = asset_id("sha256:00")
    bb._check("PUT", "/media/assets", None,
              {"parent_asset_id": parent, "range_start_sec": 0, "range_end_sec": 20.5})  # fmt: skip
    for body in (
        {"parent_asset_id": parent, "range_start_sec": 5, "range_end_sec": 5},
        {"parent_asset_id": "rec_1", "range_start_sec": 0, "range_end_sec": 5},
        {"parent_asset_id": parent, "content_hash": "sha256:00", "range_start_sec": 0,
         "range_end_sec": 5},
    ):  # fmt: skip
        with pytest.raises(ScopeViolation):
            bb._check("PUT", "/media/assets", None, body)


def test_parent_without_a_duration_fails_before_any_model_call(run) -> None:
    bb, _, _, _ = run(orgs=ORGS, ticks=0, status="dispatched")
    for doc in bb.assets.values():
        doc["data"]["technical"].pop("duration_sec")
    bb, note, report, ollama = run(add=False, orgs=ORGS)
    assert report["totals"]["failed"] == 3 and not ollama.calls and not _clips(bb)
    assert "has no technical.duration_sec in the catalog" in note
