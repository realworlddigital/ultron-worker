"""vision_tag: catalog/tag images and videos with the local Ollama vision model.

Images go to the model as they are (downscaled first when Pillow is installed); videos are
sampled into ``params.video_frames`` frames with ffmpeg and tagged from those frames together.
Without ffmpeg, videos are listed as skipped (never silently dropped).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..ollama import OllamaError, OllamaUnavailable
from . import register
from .base import (
    ItemFailed,
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

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".wmv", ".mts"}
PROMPT = (
    "You are cataloging a personal photo and video library. Look at the image(s) - for a video "
    "they are frames sampled in order - and reply with JSON only, exactly: "
    '{{"caption": "<one plain sentence>", "tags": ["<keyword>", ...]}}. '
    "Tags are short lowercase keywords: subjects, number of people, setting, activity, objects, "
    "colors, mood, and any visible text. At most {max_tags} tags."
)
FRAME_SIDE = 768
SUBPROCESS_TIMEOUT = 300


def parse_tags(raw: str, max_tags: int) -> tuple[str, list[str]]:
    caption, tags = "", []
    start, end = raw.find("{"), raw.rfind("}")
    if start > 0 and end > start:  # models sometimes wrap the JSON in prose
        raw = raw[start : end + 1]
    try:
        doc = json.loads(raw)
        if isinstance(doc, dict):
            caption = str(doc.get("caption") or "").strip()
            raw_tags = doc.get("tags") or []
            tags = (
                [str(t) for t in raw_tags]
                if isinstance(raw_tags, list)
                else str(raw_tags).split(",")
            )
    except ValueError:
        tags = raw.replace("\n", ",").split(",")
    out: list[str] = []
    for t in tags:
        t = t.strip().strip("#\"'").lower()
        if t and t not in out and len(t) <= 60:
            out.append(t)
    return caption, out[:max_tags]


class VisionTag(JobKind):
    name = "vision_tag"

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

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        path = Path(item.payload["path"])
        max_tags = int(spec.params.get("max_tags", 25))
        prompt = str(spec.params.get("prompt") or PROMPT).format(max_tags=max_tags)
        scratch = ctx.work_dir / "frames" / hashlib.sha1(item.key.encode()).hexdigest()[:16]
        try:
            if item.payload["media"] == "video":
                images = self._frames(path, scratch, int(spec.params.get("video_frames", 4)))
            else:
                images = [self._image(path, scratch)]
            try:
                raw = ctx.ollama.generate(
                    spec.model,
                    prompt,
                    images=images,
                    json_format=True,
                    options=spec.params.get("options"),
                )
            except OllamaUnavailable:
                raise
            except OllamaError as exc:
                raise ItemFailed(str(exc)) from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        caption, tags = parse_tags(raw, max_tags)
        if not tags and not caption:
            raise ItemFailed("model returned no tags")
        result: dict[str, Any] = {
            "path": str(path),
            "media": item.payload["media"],
            "caption": caption,
            "tags": tags,
        }
        if item.payload["media"] == "video":
            result["frames"] = len(images)
        return result

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

    def _frames(self, path: Path, scratch: Path, n: int) -> list[Path]:
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise ItemFailed("video skipped: ffmpeg/ffprobe not found on PATH")
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
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                check=True,
            )
            duration = float(probe.stdout.strip() or 0)
        except (subprocess.SubprocessError, ValueError) as exc:
            raise ItemFailed(f"unreadable video: {exc}") from exc
        scratch.mkdir(parents=True, exist_ok=True)
        frames = []
        for i in range(max(1, n)):
            at = duration * (i + 1) / (n + 1) if duration > 0 else 0
            out = scratch / f"frame{i:02d}.jpg"
            with contextlib.suppress(subprocess.SubprocessError):
                subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-y",
                        "-ss",
                        f"{at:.2f}",
                        "-i",
                        str(path),
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale='min({FRAME_SIDE},iw)':-2",
                        str(out),
                    ],
                    capture_output=True,
                    timeout=SUBPROCESS_TIMEOUT,
                    check=True,
                )
            if out.is_file():
                frames.append(out)
        if not frames:
            raise ItemFailed("no frames could be extracted")
        return frames

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        out = ctx.allowed(spec.output, what="output target")
        if out.is_dir():
            raise JobError(f"output target {str(out)!r} is a folder; name a .json/.jsonl file")
        tagged = [r for _, r in results if "skipped" not in r]
        skipped = [r for _, r in results if "skipped" in r]
        write_records(
            out,
            {
                "job": self.name,
                "task_id": ctx.task_id,
                "model": spec.model,
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "tagged": len(tagged),
                "skipped": len(skipped),
            },
            [r for _, r in results],
        )
        return Output(paths=[str(out)], summary=f"{len(tagged)} tagged, {len(skipped)} skipped")


register(VisionTag())
