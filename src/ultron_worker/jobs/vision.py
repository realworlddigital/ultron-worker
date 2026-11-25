"""vision_tag: catalog/tag images and videos with the local Ollama vision model.

Images go to the model as they are (downscaled first when Pillow is installed); videos are
copied once into the item's scratch dir, sampled into ``params.video_frames`` frames with ffmpeg
from that local copy and tagged from those frames together. A frame call that times out or
fails is logged and counted, never silently dropped.
Without ffmpeg, videos are listed as skipped (never silently dropped).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..catalog import file_stamp, has_stamp, stamp_matches, update_catalog
from ..media import load_index
from ..ollama import OllamaError, OllamaModelMissing, OllamaUnavailable
from . import register
from .base import (
    SCRATCH_DIR,
    ItemFailed,
    ItemRetry,
    JobContext,
    JobError,
    JobKind,
    JobSpec,
    Output,
    WorkItem,
    file_key,
    iter_files,
    write_records,
)

log = logging.getLogger("ultron")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".wmv", ".mts"}
PROMPT = (
    "You are cataloging a personal photo and video library. Look at the image(s) - for a video "
    "they are frames sampled in order - and reply with JSON only, exactly: "
    '{{"caption": "<one specific sentence>", "tags": ["<keyword>", ...]}}. '
    "The caption says concretely who is doing what, with what, where; copy any readable text "
    "(signs, logos, shirts, screens) exactly. "
    "Tags are short lowercase keywords for concrete things only: objects, activities, equipment, "
    "clothing, animals, specific places, and any readable text. Give as many as truly apply, up "
    "to {max_tags}. Never tag generic people words (man, woman, person, one person, people) - "
    "tag what they are doing or holding instead; never tag setting filler (indoor, outdoor) or "
    "mood/quality words (serious, focus, happy, calm)."
)
REPAIR = (
    "\n\nYour previous reply could not be used ({reason}); it began: {previous}. Reply again "
    "with ONE JSON object only - no prose, no code fence - exactly "
    '{{"caption": "<one specific sentence>", "tags": ["<keyword>", ...]}}, where "caption" is '
    'a non-empty sentence and "tags" is a JSON array of short keyword strings.'
)
FRAME_SIDE = 768
FRAME_TIMEOUT = 90  # per local ffprobe/ffmpeg call: slower than this is broken, not slow
CAPTION_MAX = 600
TAG_MAX = 40
TAG_MAX_WORDS = 5
TAG_JUNK = set('{}[]":\\\n\t')
# Dropped whatever the prompt says: they describe nearly every frame, so they find nothing.
FILLER_TAGS = frozenset(
    {
        # generic people descriptors
        "man", "men", "woman", "women", "person", "persons", "people", "human", "humans",
        "adult", "adults", "guy", "guys", "individual", "someone", "group of people",
        # setting filler
        "indoor", "indoors", "outdoor", "outdoors", "inside", "outside", "interior",
        "exterior", "background", "foreground", "scene", "setting",
        # mood / quality words
        "serious", "focus", "focused", "focusing", "concentration", "concentrated", "calm",
        "happy", "happiness", "joy", "joyful", "mood", "emotion", "emotional", "casual",
        "relaxed", "excited", "excitement", "energetic", "peaceful", "cozy", "beautiful",
        "nice", "candid", "blurry", "blur", "blurred", "quality",
    }
)  # fmt: skip
_PEOPLE_COUNT = re.compile(
    r"(a|one|single|two|three|four|five|six|several|many|multiple|few|\d+)\s+"
    r"(person|persons|people|man|men|woman|women|adults?|individuals?)"
)


class TagParseError(ValueError):
    """The model's reply isn't a usable {"caption", "tags"} record."""


def _clean_tag(tag: str) -> str | None:
    t = " ".join(tag.split()).strip("#\"'.,;!? ").lower()
    if (
        not t
        or len(t) > TAG_MAX
        or len(t.split()) > TAG_MAX_WORDS
        or TAG_JUNK & set(t)
        or t in FILLER_TAGS
        or _PEOPLE_COUNT.fullmatch(t)
    ):
        return None
    return t


def _json_object(raw: str) -> dict[str, Any]:
    start = raw.find("{")  # models sometimes wrap the JSON in prose or a code fence
    if start < 0:
        raise TagParseError("no JSON object in the reply")
    try:
        doc, _ = json.JSONDecoder().raw_decode(raw, start)
    except ValueError as exc:
        raise TagParseError(f"reply is not valid JSON: {getattr(exc, 'msg', exc)}") from exc
    if not isinstance(doc, dict):
        raise TagParseError("reply is not a JSON object")
    return doc


def parse_tags(raw: str, max_tags: int) -> tuple[str, list[str]]:
    """Strictly parse a reply into (caption, tags); raise :class:`TagParseError` unless it holds
    a non-empty caption and a JSON array with at least one usable tag. Never falls back to
    treating the raw text as tags."""
    doc = _json_object(raw)
    caption = doc.get("caption")
    if not isinstance(caption, str) or not caption.strip():
        raise TagParseError('"caption" is missing or empty')
    caption = " ".join(caption.split())
    if len(caption) > CAPTION_MAX or caption.startswith(("{", "[")) or '"tags"' in caption:
        raise TagParseError('"caption" is not a plain sentence')
    raw_tags = doc.get("tags")
    if not isinstance(raw_tags, list):
        raise TagParseError(f'"tags" is {type(raw_tags).__name__}, not a JSON array')
    tags: list[str] = []
    for t in raw_tags:
        clean = _clean_tag(t) if isinstance(t, str) else None
        if clean and clean not in tags:
            tags.append(clean)
    if not tags:
        raise TagParseError('"tags" has no usable keywords')
    return caption, tags[:max_tags]


def is_tag_record(result: dict[str, Any]) -> bool:
    """A checkpointed result is a well-formed tag record (or an explicit skip)."""
    if "skipped" in result:
        return True
    caption, tags = result.get("caption"), result.get("tags")
    return (
        "failed" not in result
        and isinstance(caption, str)
        and bool(caption.strip())
        and isinstance(tags, list)
        and bool(tags)
        and all(isinstance(t, str) and t and len(t) <= 60 and not TAG_JUNK & set(t) for t in tags)
    )


class VisionTag(JobKind):
    name = "vision_tag"

    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        super().validate(spec, ctx)
        for ref in _merge_refs(spec):
            if not ctx.allowed(ref, what="merge index").is_file():
                raise JobError(f"merge index {ref!r} does not exist on this machine")

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        exts = set(IMAGE_EXTS)
        if spec.params.get("include_video", True):
            exts |= VIDEO_EXTS
        files = iter_files(
            spec.inputs, ctx, exts, recursive=bool(spec.params.get("recursive", True))
        )
        return [
            WorkItem(
                key=file_key(f),
                label=str(f),
                payload={
                    "path": str(f),
                    "media": "video" if f.suffix.lower() in VIDEO_EXTS else "image",
                },
            )
            for f in files
        ]

    def seed(
        self, spec: JobSpec, items: list[WorkItem], ctx: JobContext
    ) -> dict[str, dict[str, Any]]:
        """Files the job's existing output index already has tagged and that haven't changed
        since (same size + mtime) are done. A record without a stamp (written before stamps
        existed) is trusted once and backfilled with the file's current stat."""
        ctx.cache["seeded"], ctx.cache["backfilled"] = 0, 0
        if spec.params.get("ignore_index"):
            return {}
        out = ctx.allowed(spec.output, what="output target")
        if not out.is_file():
            return {}
        roots = ctx.media_roots
        try:
            index = load_index(out, roots)
        except (OSError, ValueError) as exc:
            log.warning("output index %s can't be read, nothing seeded from it: %s", out, exc)
            return {}
        known = {roots.identity(r): r for r in index if "skipped" not in r and is_tag_record(r)}
        seeded: dict[str, dict[str, Any]] = {}
        for item in items:
            path = item.payload["path"]
            record = known.get(roots.identity({"path": path}))
            if record is None:
                continue
            try:
                stamp = file_stamp(path)
            except OSError:
                continue
            if has_stamp(record):
                if not stamp_matches(record, stamp):
                    continue  # the file changed since it was tagged: tag it again
            else:
                record = {**record, **stamp}
                ctx.cache["backfilled"] += 1
            seeded[item.key] = {**record, "path": path}
        ctx.cache["seeded"] = len(seeded)
        return seeded

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        path = Path(item.payload["path"])
        try:
            with ctx.phase("stat"):
                stamp = file_stamp(path)  # before reading: a changed file re-tags next time
        except OSError as exc:
            raise ItemFailed(f"unreadable file: {exc}") from exc
        max_tags = int(spec.params.get("max_tags", 25))
        prompt = str(spec.params.get("prompt") or PROMPT).format(max_tags=max_tags)
        scratch = ctx.work_dir / SCRATCH_DIR / hashlib.sha1(item.key.encode()).hexdigest()[:16]
        try:
            if item.payload["media"] == "video":
                with ctx.phase("frames"):
                    n = int(spec.params.get("video_frames", 4))
                    images = self._frames(path, scratch, n, ctx)
            else:
                with ctx.phase("image"):
                    images = [self._image(path, scratch)]
            raw = self._ask(spec, ctx, prompt, images)
            try:
                caption, tags = parse_tags(raw, max_tags)
            except TagParseError as first:
                previous = json.dumps(" ".join(raw.split())[:200])
                raw = self._ask(
                    spec, ctx, prompt + REPAIR.format(reason=first, previous=previous), images
                )
                try:
                    caption, tags = parse_tags(raw, max_tags)
                except TagParseError as exc:
                    raise ItemRetry(
                        f"unusable model reply after a repair re-ask: {first}; then {exc}"
                    ) from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        result: dict[str, Any] = {
            **_location(path, ctx),
            "media": item.payload["media"],
            "caption": caption,
            "tags": tags,
        }
        if item.payload["media"] == "video":
            result["frames"] = len(images)
            result["frame_timeouts"] = ctx.timing.counts.get("frame_timeouts", 0)
        return {**result, **stamp}

    def settled(self, result: dict[str, Any]) -> bool:
        return is_tag_record(result)  # also re-runs garbage checkpointed by an older runner

    def _ask(self, spec: JobSpec, ctx: JobContext, prompt: str, images: list[Path]) -> str:
        try:
            return ctx.ollama.generate(
                spec.model,
                prompt,
                images=images,
                json_format=True,
                options=spec.params.get("options"),
            )
        except (OllamaUnavailable, OllamaModelMissing):
            raise
        except OllamaError as exc:
            raise ItemFailed(str(exc)) from exc

    def _image(self, path: Path, scratch: Path) -> Path:
        try:
            from PIL import Image  # optional: downscale big photos before sending
        except ImportError:
            return path
        try:
            with Image.open(path) as im:
                if max(im.size) <= 1600 and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                    return path
                scratch.mkdir(parents=True, exist_ok=True)
                im.thumbnail((1600, 1600))
                out = scratch / "image.jpg"
                im.convert("RGB").save(out, "JPEG", quality=88)
                return out
        except Exception as exc:  # noqa: BLE001 (any decode problem is this item's problem)
            raise ItemFailed(f"unreadable image: {exc}") from exc

    def _frames(self, path: Path, scratch: Path, n: int, ctx: JobContext) -> list[Path]:
        """Sample ``n`` frames. The video is copied into the item's scratch dir first (one
        sequential read over the share) and every probe/seek runs against that local copy:
        per-frame seeks straight off the NAS share hung until the timeout. A frame call that
        times out or fails is logged and dropped; the timeouts are counted on the item."""
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ItemFailed("video skipped: ffmpeg/ffprobe not found on PATH")
        scratch.mkdir(parents=True, exist_ok=True)
        local = scratch / f"source{path.suffix.lower()}"
        try:
            shutil.copyfile(path, local)
        except OSError as exc:
            raise ItemFailed(f"unreadable video: copy to scratch failed: {exc}") from exc
        try:
            probe = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(local),
                ],
                capture_output=True,
                text=True,
                timeout=FRAME_TIMEOUT,
                check=True,
            )
            duration = float(probe.stdout.strip() or 0)
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            raise ItemFailed(f"unreadable video: {exc}") from exc
        frames: list[Path] = []
        timeouts = failures = 0
        wanted = max(1, n)
        for i in range(wanted):
            at = duration * (i + 1) / (n + 1) if duration > 0 else 0
            out = scratch / f"frame{i:02d}.jpg"
            try:
                subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        f"{at:.2f}",
                        "-i",
                        str(local),
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale='min({FRAME_SIDE},iw)':-2",
                        str(out),
                    ],
                    capture_output=True,
                    timeout=FRAME_TIMEOUT,
                    check=True,
                )
            except subprocess.TimeoutExpired:
                timeouts += 1
                log.warning(
                    "vision_tag %s: frame %d/%d (at %.2fs) timed out after %gs; dropped",
                    path, i, wanted, at, FRAME_TIMEOUT,
                )  # fmt: skip
                continue
            except (subprocess.SubprocessError, OSError) as exc:
                failures += 1
                log.warning(
                    "vision_tag %s: frame %d/%d (at %.2fs) failed; dropped: %s",
                    path, i, wanted, at, _ffmpeg_error(exc),
                )  # fmt: skip
                continue
            if out.is_file():
                frames.append(out)
        ctx.count("frame_timeouts", timeouts)
        if not frames:
            raise ItemFailed(
                f"no frames could be extracted ({timeouts} of {wanted} frame calls timed out "
                f"after {FRAME_TIMEOUT:g}s, {failures} failed)"
            )
        return frames

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        out = ctx.allowed(spec.output, what="output target")
        if out.is_dir():
            raise JobError(f"output target {str(out)!r} is a folder; name a .json/.jsonl file")
        roots = ctx.media_roots
        earlier = []
        for ref in _merge_refs(spec):
            try:
                index = load_index(ctx.allowed(ref, what="merge index"), roots)
            except (OSError, ValueError) as exc:
                raise JobError(f"merge index {ref!r} can't be read: {exc}") from exc
            earlier.append([r if is_tag_record(r) else _failed(r) for r in index])
        this_pass = [r if is_tag_record(r) else _failed(r) for _, r in results]
        records = roots.merge(*earlier, this_pass)
        skipped = [r for r in records if "skipped" in r]
        failed = [r for r in records if "failed" in r]
        tagged = [r for r in records if "skipped" not in r and "failed" not in r]
        write_records(
            out,
            {
                "job": self.name,
                "task_id": ctx.task_id,
                "model": spec.model,
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "tagged": len(tagged),
                "skipped": len(skipped),
                "failed": len(failed),
                "media_roots": sorted({r["root"] for r in records if "root" in r}),
            },
            records,
        )
        summary = f"{len(tagged)} tagged, {len(skipped)} skipped"
        if failed:
            summary += f", {len(failed)} failed"
        if ctx.cache.get("seeded"):
            summary += f" ({ctx.cache['seeded']} unchanged since the existing index, not re-tagged)"
        if earlier:
            summary += f" (merged with {len(earlier)} earlier index(es))"
        catalog = update_catalog(
            roots,
            lambda ref: ctx.allowed(ref, what="catalog folder"),
            index_path=out,
            folder_path=_folder(spec, ctx),
            counts={
                "files": len(records),
                "tagged": len(tagged),
                "skipped": len(skipped),
                "failed": len(failed),
            },
            model=spec.model,
        )
        return Output(paths=[str(out)], summary=f"{summary}; {catalog}")


def _failed(result: dict[str, Any]) -> dict[str, Any]:
    """The only shape an unusable result reaches the output in: location + reason, no tags."""
    reason = result.get("failed") or "malformed result from an earlier run"
    out = {
        "root": result.get("root"),
        "relpath": result.get("relpath"),
        "path": result.get("path"),
        "media": result.get("media"),
        "failed": str(reason),
    }
    return {k: v for k, v in out.items() if v is not None}


def _ffmpeg_error(exc: BaseException) -> str:
    stderr = getattr(exc, "stderr", None)
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    detail = " ".join(str(stderr or "").split())[-300:]
    return f"{exc}: {detail}" if detail else str(exc)


def _location(path: Path, ctx: JobContext) -> dict[str, str]:
    """A record's identity: media root name + forward-slash relative path (the absolute
    ``path`` rides along for debugging only; readers derive it from the mapping)."""
    found = ctx.media_roots.relativize(path)
    loc = {"root": found[0], "relpath": found[1]} if found else {}
    return {**loc, "path": str(path)}


def _folder(spec: JobSpec, ctx: JobContext) -> Path | None:
    """The folder a TOC entry names: the job's input when it is one folder, else None (the
    catalog then uses the index's own folder)."""
    if len(spec.inputs) != 1:
        return None
    p = ctx.allowed(spec.inputs[0], what="input")
    return p if p.is_dir() else None


def _merge_refs(spec: JobSpec) -> list[str]:
    refs = spec.params.get("merge") or []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs):
        raise JobError("card data.job.params.merge must be a list of index paths")
    return refs


register(VisionTag())
