"""clip_segment: cut long footage into precisely-tagged virtual clips at ingestion.

A clip is a range on its source asset (apollo-backbone ``record.media_asset`` with
``parent_asset_id`` + ``range_start_sec``/``range_end_sec``, org inherited, rights from the
parent): no bytes are cut, copied or stored here. One work item per long video, the parent:

1. the parent's catalog record (``GET /records/mas_...``) must exist: ``catalog_publish``
   publishes the whole asset first;
2. org from ``params.org``/``params.orgs`` exactly as catalog_publish (deepest match wins, never
   from a name); unmapped footage is **held** (named in the report, never segmented), and a
   mapping that disagrees with the parent's org is held too (a clip can't change org);
3. one ffmpeg pass proposes boundaries: scene changes plus silence (speech breaks); segments
   are cut greedily in the target window (15-30 s), within the hard bounds (12-40 s);
4. the vision model (``job.model``, Glimmer by default) sees frames sampled from each segment
   and judges social usability, captioning and tagging what happens in THOSE seconds;
5. every kept segment is upserted through ``PUT /media/assets`` (the server derives the clip id
   from parent + range, ``mas_<sha256("clip:<parent>:<start:.3f>:<end:.3f>")[:24]>``, so a
   re-run never duplicates).

The per-asset analysis, judgements and published clips are kept in
``<work_dir>/segments/<parent>.json``, so a job killed mid-asset resumes at the next segment;
the runner's checkpoint keeps finished assets finished.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..backbone import BackboneError, BackboneUnavailable
from ..catalog import file_stamp, has_stamp, stamp_matches
from ..media import MediaRoots, _parts, read_index
from ..ollama import OllamaError, OllamaModelMissing, OllamaUnavailable
from . import register
from .base import (
    KEY_MAX,
    SCRATCH_DIR,
    ItemFailed,
    ItemRetry,
    JobContext,
    JobError,
    JobKind,
    JobSpec,
    JobStopped,
    Output,
    WorkItem,
    atomic_write_text,
)
from .publish import (
    BACKBONE_ORGS,
    STORAGE_ID,
    _better,
    _content_hash,
    _indexes,
    _now,
    _org_for,
    _orgs,
    _same,
    _under,
    asset_id,
)
from .vision import (
    FILLER_TAGS,
    FRAME_SIDE,
    FRAME_TIMEOUT,
    VIDEO_EXTS,
    TagParseError,
    VisionTag,
    _ffmpeg_error,
    _json_object,
    parse_tags,
)

log = logging.getLogger("ultron")
DEFAULT_MODEL = "muse-glimmer:30b"
FEEDER = "ultron_segmentation"
INGESTED_BY = "ultron"
ASSET_ID = re.compile(r"mas_[0-9a-f]{24}")
STATE_DIR = "segments"
STATE_VERSION = 1
ANALYSIS_SECONDS_PER_MEDIA_SECOND = 2.0  # ffmpeg analysis deadline: FRAME_TIMEOUT + this x length
POLL = 0.5
NAMED_MAX = 20
IDS_MAX = 200  # clip ids named in the note; the report has all of them
DEFAULTS = {
    "min_sec": 12.0,  # hard bounds for a kept segment
    "max_sec": 40.0,
    "target_min_sec": 15.0,  # where cuts are looked for first
    "target_max_sec": 30.0,
    "frames": 3,  # frames per segment shown to the model
    "max_tags": 15,
    "scene_threshold": 0.3,
    "silence_db": -35.0,
    "silence_min_sec": 0.6,
}
PAD = 0.2  # seconds of silence kept either side of a speech-break cut
LONG_PAUSE = 3.0  # a silence longer than this is a region edge, not a speech break
PROMPT = (
    "You are cutting a long video into short clips for social media. The images are frames "
    "sampled in order from ONE {length:.0f}-second segment ({start:.1f}s to {end:.1f}s of the "
    "video), nothing else. Reply with JSON only, exactly: "
    '{{"usable": true or false, "reason": "<a few words>", "caption": "<one specific sentence>", '
    '"tags": ["<keyword>", ...]}}. '
    "usable is true when these seconds would work as a short social clip on their own: a person "
    "mid-action or mid-sentence, something being built or working, a demonstration, a reaction. "
    "It is false for dead air, setup or teardown, an empty room, black, blurred or frozen frames. "
    "The caption says concretely who is doing what, with what, in THESE frames only; copy any "
    "readable text (signs, slides, screens, shirts) exactly. Tags are short lowercase keywords "
    "for concrete things visible in these frames: objects, activities, equipment, readable text, "
    "up to {max_tags}. Never tag generic people words (man, woman, person, people) or setting "
    "and mood filler (indoor, outdoor, serious, happy)."
)
REPAIR = (
    "\n\nYour previous reply could not be used ({reason}); it began: {previous}. Reply again "
    "with ONE JSON object only - no prose, no code fence - exactly "
    '{{"usable": true or false, "reason": "<a few words>", "caption": "<one specific sentence>", '
    '"tags": ["<keyword>", ...]}}.'
)
_SCENE = re.compile(r"pts_time:\s*([0-9.]+)")
_SIL_START = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SIL_END = re.compile(r"silence_end:\s*([0-9.]+)")


class ClipSegment(JobKind):
    name = "clip_segment"
    needs_model = False  # job.model is optional: Glimmer by default

    # ---- spec --------------------------------------------------------------------
    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        out = ctx.allowed(spec.output, what="output target")
        if out.suffix.lower() != ".json" or out.is_dir():
            raise JobError(f"output target {str(out)!r} must be a .json report file")
        if out.name == "tags.jsonl" or out.parent.name == "_index":
            raise JobError(f"output target {str(out)!r} would overwrite a catalog file")
        for raw in spec.inputs:
            if ASSET_ID.fullmatch(raw):
                continue
            p = ctx.allowed(raw, what="input")
            if not p.is_dir():
                raise JobError(f"input {raw!r} is neither a folder nor an asset id (mas_...)")
            if ctx.media_roots.relativize(p) is None:
                raise JobError(f"input {raw!r} is not under a named media root")
        _orgs(spec, ctx)
        _window(spec)
        if getattr(ctx, "backbone", None) is None:
            raise JobError("clip_segment needs the runner's backbone client")
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise JobError("clip_segment needs ffmpeg and ffprobe on PATH")

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        roots: MediaRoots = ctx.media_roots
        orgs = _orgs(spec, ctx)
        items: dict[str, WorkItem] = {}
        notes: list[str] = []
        for raw in spec.inputs:
            if ASSET_ID.fullmatch(raw):
                items.setdefault(raw, WorkItem(key=raw, label=raw, payload={"asset_id": raw}))
                continue
            root, folder = roots.relativize(ctx.allowed(raw, what="input"))  # type: ignore[misc]
            label = f"{root}/{folder}" if folder else root
            indexes = _indexes(root, folder, ctx)
            out = _same(ctx.allowed(spec.output, what="output target"))
            if any(_same(index) == out for index in indexes):
                raise JobError(f"output target {spec.output!r} is a folder index it would replace")
            if not indexes:
                raise JobError(
                    f"{label}: no folder index (tags.jsonl) in the TOC or in the folder; tag and "
                    "publish it (vision_tag, catalog_publish) before segmenting"
                )
            picked: dict[tuple[str, str], dict[str, Any]] = {}
            for index in indexes:
                try:
                    recs = read_index(index)
                except (OSError, ValueError) as exc:
                    raise JobError(f"index {str(index)!r} can't be read: {exc}") from exc
                for rec in map(roots.normalize, recs):
                    same_root = "root" in rec and roots.lookup(str(rec["root"])) == roots.lookup(
                        root
                    )
                    if not same_root or not _under(rec["relpath"], folder, roots, root):
                        continue
                    if not _video(rec):
                        continue
                    key = roots.identity(rec)
                    if key not in picked or _better(rec, picked[key]):
                        picked[key] = rec
            videos = 0
            for rec in picked.values():
                ref = f"{rec['root']}/{rec['relpath']}"
                content_hash = _content_hash(rec)
                key = asset_id(content_hash) if content_hash else f"path:{ref}"[:KEY_MAX]
                if key in items:
                    continue
                videos += 1
                items[key] = WorkItem(
                    key=key,
                    label=ref,
                    payload={
                        "asset_id": asset_id(content_hash) if content_hash else None,
                        "record": {"root": rec["root"], "relpath": rec["relpath"]},
                        **(
                            {"stamp": {k: rec[k] for k in ("size_bytes", "mtime")}}
                            if has_stamp(rec)
                            else {}
                        ),  # fmt: skip
                        "folder": label,
                        "org": _org_for(rec, orgs, roots),
                    },
                )
            notes.append(f"{label}: {videos} video(s) from {len(indexes)} index(es)")
        ctx.cache["segment.plan"] = notes
        return list(items.values())

    # ---- one asset ---------------------------------------------------------------
    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        roots: MediaRoots = ctx.media_roots
        payload = item.payload
        result: dict[str, Any] = {"path": item.label, "folder": payload.get("folder")}
        aid = payload.get("asset_id")
        if aid is None:
            return {**result, "error": "no content_hash in its index: publish it first "
                    "(catalog_publish)"}  # fmt: skip
        result["parent_asset_id"] = aid
        parent = _read(ctx, aid)
        if parent is None:
            return {**result, "error": f"parent {aid} is not in the catalog: publish it first "
                    "(catalog_publish)"}  # fmt: skip
        pdata = parent.get("data") or {}
        if pdata.get("parent_asset_id"):
            return {**result, "error": f"{aid} is itself a clip"}
        if pdata.get("kind") != "video":
            return {**result, "error": f"{aid} is a {pdata.get('kind')}, not a video"}
        rec = payload.get("record") or _record_from_locations(pdata, roots)
        if rec is None:
            return {**result, "error": f"{aid} has no verified {STORAGE_ID} location under the "
                    "configured media roots"}  # fmt: skip
        ref = f"{rec['root']}/{rec['relpath']}"
        result["path"] = ref
        org = payload.get("org") or _org_for(rec, _orgs(spec, ctx), roots)
        result["org"] = org
        if org not in BACKBONE_ORGS:
            return {**result, "held": "no org mapping for this footage; supply params.orgs"}
        if pdata.get("org") != org:
            return {**result, "held": f"mapped to {org!r} but the parent is "
                    f"{pdata.get('org')!r}; a clip always carries its parent's org"}  # fmt: skip
        where = roots.resolve(rec["root"], rec["relpath"])
        path = ctx.allowed(where, what="media file") if where else None
        if path is None or not path.is_file():
            return {**result, "error": "file missing on disk"}
        stamp = file_stamp(path)
        # The file must still be the bytes the parent was published from: a replaced take would
        # put its ranges and captions on the old asset.
        size = (pdata.get("technical") or {}).get("size_bytes")
        tagged = payload.get("stamp")
        if (tagged and not stamp_matches(tagged, stamp)) or (
            isinstance(size, int) and size != stamp["size_bytes"]
        ):
            return {**result, "error": "file changed since it was published; re-tag and "
                    "re-publish it (vision_tag, catalog_publish) before segmenting"}  # fmt: skip
        # The backbone bounds every range by the parent's own duration: without one no clip
        # could be written, so don't spend model time on it.
        duration = _num((pdata.get("technical") or {}).get("duration_sec"))
        if not duration:
            return {**result, "error": f"parent {aid} has no technical.duration_sec in the "
                    "catalog: re-publish it (catalog_publish probes it)"}  # fmt: skip
        result["duration_sec"] = round(duration, 3)
        win = _window(spec)
        if duration <= win["max_sec"]:
            return {**result, "short": True, "proposed": 0, "kept": 0, "clips": []}

        state_path = ctx.work_dir / STATE_DIR / f"{aid}.json"
        state = _load_state(state_path, stamp, _settings(spec, win))
        scratch = ctx.work_dir / SCRATCH_DIR / hashlib.sha1(item.key.encode()).hexdigest()[:16]
        local: list[Path] = []  # the scratch copy, made on first need

        def source() -> Path:
            if not local:
                scratch.mkdir(parents=True, exist_ok=True)
                copy = scratch / f"source{path.suffix.lower()}"
                with ctx.phase("copy"):
                    VisionTag()._copy(path, copy, ctx)
                local.append(copy)
            return local[0]

        try:
            if state.get("segments") is None:
                with ctx.phase("analysis"):
                    scenes, silences = _analyse(source(), duration, spec, ctx)
                state["segments"] = [
                    {"start": s, "end": e, "cut": cut}
                    for s, e, cut in propose(duration, scenes, silences, **win)
                ]
                state["scenes"], state["silences"] = len(scenes), len(silences)
                _save_state(state_path, state)
            model = spec.model or DEFAULT_MODEL
            retry: list[str] = []
            for seg in state["segments"]:
                if seg.get("verdict") is not None:
                    continue
                if ctx.stop.is_set():
                    raise JobStopped
                seg_scratch = scratch / f"seg_{seg['start']:.2f}"
                try:
                    frames = _frames(source(), seg, seg_scratch, int(win["frames"]), ctx)
                    if not frames:
                        seg.update(verdict="rejected", reason="no frames could be extracted")
                    else:
                        seg.update(_judge(spec, ctx, model, seg, frames))
                except ItemRetry as exc:
                    retry.append(f"{seg['start']:.1f}-{seg['end']:.1f}s: {exc}")
                    continue
                finally:
                    shutil.rmtree(seg_scratch, ignore_errors=True)
                _save_state(state_path, state)
            for seg in state["segments"]:
                if seg.get("verdict") == "kept" and not seg.get("clip_id"):
                    seg.update(_publish(_clip(aid, pdata, seg, model, ctx), ctx))
                    _save_state(state_path, state)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if retry:
            raise ItemRetry(
                f"{len(retry)} segment(s) got no usable verdict (rest checkpointed): {retry[0]}"
            )
        segs = state["segments"]
        kept = [s for s in segs if s.get("verdict") == "kept"]
        return {
            **result,
            "model": spec.model or DEFAULT_MODEL,
            "scenes": state.get("scenes", 0),
            "silences": state.get("silences", 0),
            "proposed": len(segs),
            "kept": len(kept),
            "clips": [
                {
                    k: s.get(k)
                    for k in ("clip_id", "start", "end", "outcome", "caption", "error")
                    if s.get(k) is not None
                }
                for s in kept
            ],  # fmt: skip
            "rejected": [
                {"start": s["start"], "end": s["end"], "reason": s.get("reason")}
                for s in segs
                if s.get("verdict") != "kept"
            ],
        }

    # ---- result ------------------------------------------------------------------
    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        out = ctx.allowed(spec.output, what="output target")
        totals = dict.fromkeys(
            ("assets", "short", "proposed", "kept", "held", "failed", "created", "updated",
             "unchanged", "clip_failed"), 0)  # fmt: skip
        named: dict[str, list[str]] = {"held": [], "failed": [], "created": [], "clip_failed": []}
        rows: list[dict[str, Any]] = []
        lines: list[str] = []
        for item, res in results:
            totals["assets"] += 1
            label = res.get("path") or item.label
            row = {**res, "path": label}
            if "held" in res:
                totals["held"] += 1
                named["held"].append(f"{label} ({res['held']})")
                row["outcome"] = "held"
            elif "error" in res or "skipped" in res or "failed" in res:
                totals["failed"] += 1
                why = res.get("error") or res.get("skipped") or res.get("failed")
                named["failed"].append(f"{label} ({why})")
                row["outcome"] = "failed"
            elif res.get("short"):
                totals["short"] += 1
                row["outcome"] = "short"
            else:
                row["outcome"] = "segmented"
                totals["proposed"] += res.get("proposed", 0)
                totals["kept"] += res.get("kept", 0)
                counts = dict.fromkeys(("created", "updated", "unchanged"), 0)
                for clip in res.get("clips", []):
                    outcome = clip.get("outcome")
                    if outcome in counts:
                        counts[outcome] += 1
                        totals[outcome] += 1
                    else:
                        totals["clip_failed"] += 1
                        named["clip_failed"].append(
                            f"{label} {clip['start']:.2f}-{clip['end']:.2f}s ({clip.get('error')})"
                        )
                    if outcome == "created":
                        named["created"].append(clip["clip_id"])
                lines.append(
                    f"- {label} ({res.get('parent_asset_id')}, {res.get('duration_sec', 0):.0f}s): "
                    f"{res.get('proposed', 0)} proposed, {res.get('kept', 0)} kept; "
                    f"{counts['created']} created, {counts['updated']} updated, "
                    f"{counts['unchanged']} unchanged"
                )
            rows.append(row)
        report = {
            "kind": self.name,
            "task_id": ctx.task_id,
            "model": spec.model or DEFAULT_MODEL,
            "window": _window(spec),
            "generated_at": _now(),
            "totals": totals,
            "clip_ids_created": named["created"],
            "assets": rows,
        }
        atomic_write_text(out, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        head = (
            f"Segmented {totals['assets']} asset(s) into virtual clips: "
            f"{totals['proposed']} segments proposed, {totals['kept']} kept, "
            f"{totals['held']} held (no org), {totals['short']} too short, "
            f"{totals['failed']} failed; clips {totals['created']} created, "
            f"{totals['updated']} updated, {totals['unchanged']} unchanged"
            + (f", {totals['clip_failed']} not published" if totals["clip_failed"] else "")
        )
        detail = [*lines, *(f"Plan: {n}" for n in ctx.cache.get("segment.plan", []))]
        for key, what, cap in (
            ("held", "Held, not segmented (no backbone org)", NAMED_MAX),
            ("failed", "Not segmented (failed)", NAMED_MAX),
            ("clip_failed", "Clips not published", NAMED_MAX),
            ("created", "Clip ids created", IDS_MAX),
        ):
            if named[key]:
                more = len(named[key]) - cap
                detail.append(
                    f"{what}: {len(named[key])}: "
                    + "; ".join(named[key][:cap])
                    + (f"; ... {more} more in the report" if more > 0 else "")
                )
        return Output(paths=[str(out)], summary="\n".join([head, *detail]))


# ---- segmentation ------------------------------------------------------------------
def _window(spec: JobSpec) -> dict[str, float]:
    win: dict[str, float] = {}
    for key, default in DEFAULTS.items():
        value = spec.params.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= -1000:
            raise JobError(f"params.{key} must be a number")
        win[key] = float(value)
    ok = 0 < win["min_sec"] <= win["target_min_sec"] <= win["target_max_sec"] <= win["max_sec"]
    if not ok:
        raise JobError(
            "params need 0 < min_sec <= target_min_sec <= target_max_sec <= max_sec "
            f"(got {win['min_sec']:g}, {win['target_min_sec']:g}, {win['target_max_sec']:g}, "
            f"{win['max_sec']:g})"
        )
    if not 1 <= win["frames"] <= 8:
        raise JobError("params.frames must be 1-8")
    return {k: win[k] for k in ("min_sec", "max_sec", "target_min_sec", "target_max_sec",
                                "frames")}  # fmt: skip


def propose(
    duration: float,
    scenes: list[float],
    silences: list[tuple[float, float]],
    *,
    min_sec: float,
    max_sec: float,
    target_min_sec: float,
    target_max_sec: float,
    frames: float = 0,
) -> list[tuple[float, float, str]]:
    """Cut ``[0, duration]`` into (start, end, cut) segments of min_sec..max_sec.

    A silence longer than LONG_PAUSE splits the video into regions at its edges, so a talking
    clip never runs into a long silent stretch; the silent stretch is a region of its own, cut
    and judged like any other (silence is not dead air: the vision model decides that). A
    shorter pause is a speech break: the segment ends just after it starts and the next begins
    just before it ends. Within a region each next cut is looked for in the target window
    first, then anywhere in the hard bounds, preferring a speech break over a scene change and
    the candidate nearest the middle of the target window; with neither, a hard cut at
    target_max_sec (``cut: forced``). A region's tail shorter than min_sec joins its last
    segment when that stays within max_sec, else it is dropped (as is a region shorter than
    min_sec). Deterministic: the same analysis always gives the same ranges (and so ids)."""
    edges = {0.0, duration}
    breaks: list[tuple[float, float, str]] = []  # (end here, next starts here, kind)
    for a, b in silences:
        if b - a > LONG_PAUSE:
            edges |= {min(max(a + PAD, 0.0), duration), min(max(b - PAD, 0.0), duration)}
        elif b - a >= 2 * PAD:
            breaks.append((a + PAD, b - PAD, "speech_break"))
    breaks += [(t, t, "scene") for t in scenes]
    breaks.sort()
    bounds = sorted(edges)
    out: list[tuple[float, float, str]] = []
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        last = "end" if hi >= duration else "silence"
        inside = [c for c in breaks if lo < c[0] and c[1] < hi]
        out += _cut(lo, hi, last, inside, min_sec, max_sec, target_min_sec, target_max_sec)
    end = math.floor(duration * 100) / 100  # a range never ends past the parent
    return [(round(s, 2), min(round(e, 2), end), cut) for s, e, cut in out]


def _cut(
    lo: float, hi: float, last: str, breaks: list[tuple[float, float, str]], min_sec: float,
    max_sec: float, target_min_sec: float, target_max_sec: float,
) -> list[tuple[float, float, str]]:  # fmt: skip
    """The greedy cut of one region ``[lo, hi]`` (see :func:`propose`)."""
    mid = (target_min_sec + target_max_sec) / 2
    out: list[tuple[float, float, str]] = []
    start = lo
    while hi - start >= min_sec:
        if hi - start <= max_sec:
            out.append((start, hi, last))
            return out
        best = None
        for wlo, whi in ((target_min_sec, target_max_sec), (min_sec, max_sec)):
            found = [c for c in breaks if start + wlo <= c[0] <= start + whi]
            if found:
                best = min(
                    found, key=lambda c: (c[2] != "speech_break", abs(c[0] - start - mid), c[0])
                )
                break
        end, nxt, cut = best or (start + target_max_sec, start + target_max_sec, "forced")
        out.append((start, end, cut))
        start = max(nxt, end)
    if out and hi > start and hi - out[-1][0] <= max_sec:
        out.append((out.pop()[0], hi, last))
    return out


def _analyse(
    local: Path, duration: float, spec: JobSpec, ctx: JobContext
) -> tuple[list[float], list[tuple[float, float]]]:
    """One ffmpeg pass: scene-change times (video downscaled to 5 fps) and silence intervals."""
    thr = float(spec.params.get("scene_threshold", DEFAULTS["scene_threshold"]))
    db = float(spec.params.get("silence_db", DEFAULTS["silence_db"]))
    dur = float(spec.params.get("silence_min_sec", DEFAULTS["silence_min_sec"]))
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostats", "-i", str(local),
        "-vf", f"fps=5,scale=320:-2,select='gt(scene,{thr:g})',showinfo",
        "-af", f"silencedetect=noise={db:g}dB:d={dur:g}",
        "-f", "null", "-",
    ]  # fmt: skip
    stderr = _run(cmd, FRAME_TIMEOUT + duration * ANALYSIS_SECONDS_PER_MEDIA_SECOND, ctx)
    return parse_analysis(stderr, duration)


def parse_analysis(stderr: str, duration: float) -> tuple[list[float], list[tuple[float, float]]]:
    scenes: list[float] = []
    silences: list[tuple[float, float]] = []
    open_at: float | None = None
    for line in stderr.splitlines():
        if "showinfo" in line and (m := _SCENE.search(line)):
            t = float(m.group(1))
            if 0 < t < duration and (not scenes or t - scenes[-1] >= 0.5):
                scenes.append(round(t, 2))
        elif m := _SIL_START.search(line):
            open_at = max(0.0, float(m.group(1)))
        elif (m := _SIL_END.search(line)) and open_at is not None:
            silences.append((round(open_at, 2), round(min(float(m.group(1)), duration), 2)))
            open_at = None
    if open_at is not None and open_at < duration:
        silences.append((round(open_at, 2), round(duration, 2)))
    return scenes, silences


def _run(cmd: list[str], deadline: float, ctx: JobContext) -> str:
    """Run ffmpeg in a child with a deadline, killed on a runner stop; its stderr."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        raise JobError(f"ffmpeg can't start: {exc}", retryable=True) from exc
    started = time.monotonic()
    try:
        while True:
            try:
                _, err = proc.communicate(timeout=POLL)
                break
            except subprocess.TimeoutExpired:
                if ctx.stop.is_set():
                    raise JobStopped from None
                if time.monotonic() - started >= deadline:
                    raise ItemFailed(f"ffmpeg analysis timed out after {deadline:.0f}s") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=POLL)
    text = err.decode(errors="replace")
    if proc.returncode != 0:
        raise ItemFailed(f"ffmpeg analysis failed: {' '.join(text.split())[-300:]}")
    return text


def _frames(local: Path, seg: dict[str, Any], scratch: Path, n: int, ctx: JobContext) -> list[Path]:
    """``n`` frames evenly inside the segment, from the local copy."""
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    scratch.mkdir(parents=True, exist_ok=True)
    start, end = float(seg["start"]), float(seg["end"])
    frames: list[Path] = []
    for i in range(n):
        at = start + (end - start) * (i + 0.5) / n
        out = scratch / f"frame{i:02d}.jpg"
        with ctx.phase("frames"):
            try:
                subprocess.run(
                    [ffmpeg, "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(local),
                     "-frames:v", "1", "-vf", f"scale='min({FRAME_SIDE},iw)':-2", str(out)],
                    capture_output=True, timeout=FRAME_TIMEOUT, check=True,
                )  # fmt: skip
            except (subprocess.SubprocessError, OSError) as exc:
                log.warning("clip_segment %s: frame at %.2fs dropped: %s", local, at,
                            _ffmpeg_error(exc))  # fmt: skip
                continue
        if out.is_file():
            frames.append(out)
    return frames


def parse_verdict(raw: str, max_tags: int) -> dict[str, Any]:
    """A reply as {verdict, reason, caption, tags}. A kept segment needs a caption and usable
    tags (vision_tag's strict rules); a rejected one only its reason."""
    doc = _json_object(raw)
    usable = doc.get("usable")
    if not isinstance(usable, bool):
        raise TagParseError('"usable" is missing or not true/false')
    reason = " ".join(str(doc.get("reason") or "").split())[:200] or None
    if not usable:
        return {"verdict": "rejected", "reason": reason or "not usable"}
    caption, tags = parse_tags(raw, max_tags)
    return {"verdict": "kept", "reason": reason, "caption": caption[:200], "tags": tags}


def _judge(
    spec: JobSpec, ctx: JobContext, model: str, seg: dict[str, Any], frames: list[Path]
) -> dict[str, Any]:
    max_tags = int(spec.params.get("max_tags", DEFAULTS["max_tags"]))
    start, end = float(seg["start"]), float(seg["end"])
    prompt = str(spec.params.get("prompt") or PROMPT).format(
        length=end - start, start=start, end=end, max_tags=max_tags
    )

    def ask(text: str) -> str:
        try:
            return ctx.ollama.generate(
                model, text, images=frames, json_format=True, options=spec.params.get("options")
            )
        except (OllamaUnavailable, OllamaModelMissing):
            raise
        except OllamaError as exc:
            raise ItemRetry(str(exc)) from exc

    raw = ask(prompt)
    try:
        return parse_verdict(raw, max_tags)
    except TagParseError as first:
        previous = json.dumps(" ".join(raw.split())[:200])
        raw = ask(prompt + REPAIR.format(reason=first, previous=previous))
        try:
            return parse_verdict(raw, max_tags)
        except TagParseError as exc:
            raise ItemRetry(
                f"unusable model reply after a repair re-ask: {first}; then {exc}"
            ) from exc


# ---- the clip record ---------------------------------------------------------------
def _clip(
    parent_id: str, parent: dict[str, Any], seg: dict[str, Any], model: str, ctx: JobContext
) -> dict[str, Any]:
    """The record.media_asset clip: tags describe the range only (never the parent's caption);
    org and kind repeat the parent's; rights stay null (the parent's apply). No technical,
    content_hash or locations: the backbone derives the range's technical from the parent."""
    keywords = [t for t in seg.get("tags") or [] if t not in FILLER_TAGS]
    structured: dict[str, Any] = {"caption": seg["caption"], "keywords": keywords}
    if seg.get("reason"):
        structured["usable_because"] = str(seg["reason"])[:200]
    structured["segmented_by"] = model[:200]
    return {
        "parent_asset_id": parent_id,
        "range_start_sec": seg["start"],
        "range_end_sec": seg["end"],
        "kind": parent.get("kind", "video"),
        "tags": {"structured": structured, "flat": [k.lower()[:80] for k in keywords][:200]},
        "provenance": {
            "feeder": FEEDER,
            "ingested_by": INGESTED_BY,
            "ingested_at": _now(),
            "source_ref": ctx.task_id,
        },
        "rights": None,
        "org": parent["org"],
    }


def _publish(body: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Upsert one clip: the server derives its id from parent + range, so the same range
    always lands on the same record. A backbone outage parks the job (this asset's earlier
    segments are already in its state file); a refused key stops it; any other refusal
    fails just this clip."""
    try:
        with ctx.phase("backbone"):
            res = ctx.backbone.put_media_clip(body) or {}
    except BackboneUnavailable as exc:
        raise JobError(
            f"backbone unreachable mid-segmentation ({exc}); progress is saved and the job "
            "resumes at this clip",
            retryable=True,
        ) from exc
    except BackboneError as exc:
        if exc.status in (401, 403):
            raise JobError(f"backbone refused the runner's key for media clips: {exc}") from exc
        return {"outcome": "failed", "error": str(exc)[:300]}
    doc = res.get("doc") or {}
    if not isinstance(doc.get("id"), str):
        return {"outcome": "failed", "error": "backbone answered without a record id"}
    if res.get("replayed"):
        outcome = "unchanged"
    else:
        outcome = "created" if doc.get("version") == 1 else "updated"
    return {"clip_id": doc["id"], "outcome": outcome}


# ---- helpers -----------------------------------------------------------------------
def _read(ctx: JobContext, aid: str) -> dict[str, Any] | None:
    try:
        with ctx.phase("backbone"):
            return ctx.backbone.media_asset(aid)
    except BackboneUnavailable as exc:
        raise JobError(
            f"backbone unreachable ({exc}); the job resumes at this asset", retryable=True
        ) from exc
    except BackboneError as exc:
        if exc.status in (401, 403):
            raise JobError(f"backbone refused the runner's key for media assets: {exc}") from exc
        raise JobError(f"backbone refused reading {aid}: {exc}", retryable=True) from exc


def _record_from_locations(data: dict[str, Any], roots: MediaRoots) -> dict[str, Any] | None:
    for loc in data.get("locations") or []:
        if loc.get("storage_id") != STORAGE_ID or loc.get("status") != "verified":
            continue
        parts = _parts(str(loc.get("path") or ""))
        if len(parts) >= 2 and roots.lookup(parts[0]):
            return {"root": roots.lookup(parts[0]), "relpath": "/".join(parts[1:])}
    return None


def _video(rec: dict[str, Any]) -> bool:
    if rec.get("media") in ("image", "audio"):
        return False
    return rec.get("media") == "video" or Path(str(rec.get("relpath"))).suffix.lower() in VIDEO_EXTS


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def _settings(spec: JobSpec, win: dict[str, float]) -> dict[str, Any]:
    """Everything a saved analysis and its verdicts depend on: a change re-runs them."""
    analysis = {k: spec.params.get(k, DEFAULTS[k])
                for k in ("scene_threshold", "silence_db", "silence_min_sec")}  # fmt: skip
    return json.loads(json.dumps({
        **win, **analysis, "model": spec.model or DEFAULT_MODEL,
        "prompt": spec.params.get("prompt"), "options": spec.params.get("options"),
        "max_tags": spec.params.get("max_tags", DEFAULTS["max_tags"]),
    }, default=str))  # fmt: skip


def _load_state(path: Path, stamp: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """This asset's saved progress, unless the file or the settings changed since."""
    with contextlib.suppress(OSError, ValueError):
        state = json.loads(path.read_text(encoding="utf-8"))
        same = (
            isinstance(state, dict)
            and state.get("v") == STATE_VERSION
            and state.get("settings") == settings
            and has_stamp(state.get("stamp") or {})
            and stamp_matches(state["stamp"], stamp)
        )
        if same:
            return state
    return {"v": STATE_VERSION, "stamp": stamp, "settings": settings, "segments": None}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(state, ensure_ascii=False) + "\n")


register(ClipSegment())
