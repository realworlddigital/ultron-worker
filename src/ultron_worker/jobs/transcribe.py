"""transcribe: speech-to-text with faster-whisper, when it is installed
(``pip install ultron-worker[transcribe]``). ``model`` is a faster-whisper model name
(e.g. ``large-v3``, ``medium``); ``output`` is a folder that receives ``<name>.txt`` and
``<name>.segments.json`` per input plus an ``index.json``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import register
from .base import (
    ItemFailed,
    JobContext,
    JobError,
    JobKind,
    JobSpec,
    Output,
    WorkItem,
    atomic_write_text,
    file_key,
    iter_files,
)

MEDIA_EXTS = {
    ".wav",
    ".mp3",
    ".m4a",
    ".flac",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".webm",
}


def available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class Transcribe(JobKind):
    name = "transcribe"

    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        if not available():
            raise JobError(
                "faster-whisper is not installed on Ultron: run "
                "`pip install ultron-worker[transcribe]` in the runner's venv"
            )
        for i in spec.inputs:
            if not ctx.allowed(i, what="input").exists():
                raise JobError(f"input {i!r} does not exist on this machine")
        out = ctx.allowed(spec.output, what="output target")
        if out.exists() and not out.is_dir():
            raise JobError("transcribe writes a folder: output must be a folder path")

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        files = iter_files(
            spec.inputs, ctx, MEDIA_EXTS, recursive=bool(spec.params.get("recursive", True))
        )
        stems = Counter(f.stem for f in files)
        items = []
        for f in files:
            name = (
                f.stem
                if stems[f.stem] == 1
                else f"{f.stem}-{hashlib.sha1(str(f).encode()).hexdigest()[:8]}"
            )
            items.append(
                WorkItem(key=file_key(f), label=str(f), payload={"path": str(f), "name": name})
            )
        return items

    def _model(self, spec: JobSpec, ctx: JobContext) -> Any:
        if "whisper" not in ctx.cache:
            from faster_whisper import WhisperModel

            ctx.cache["whisper"] = WhisperModel(
                spec.model,
                device=str(spec.params.get("device", "auto")),
                compute_type=str(spec.params.get("compute_type", "int8")),
            )
        return ctx.cache["whisper"]

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        out_dir = ctx.allowed(spec.output, what="output target")
        model = self._model(spec, ctx)  # a model that won't load fails the job, not the item
        try:
            segments, info = model.transcribe(
                item.payload["path"],
                language=spec.params.get("language"),
                beam_size=int(spec.params.get("beam_size", 5)),
                vad_filter=True,
            )
            segs = []
            for s in segments:
                if ctx.stop.is_set():
                    raise JobError("stopping", retryable=True)
                segs.append(
                    {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
                )
        except (JobError, ItemFailed):
            raise
        except Exception as exc:  # noqa: BLE001 (decoder errors are this file's problem)
            raise ItemFailed(f"transcription failed: {exc}") from exc
        name = item.payload["name"]
        txt, js = out_dir / f"{name}.txt", out_dir / f"{name}.segments.json"
        atomic_write_text(txt, "\n".join(s["text"] for s in segs) + "\n")
        atomic_write_text(
            js,
            json.dumps(
                {
                    "source": item.payload["path"],
                    "language": getattr(info, "language", None),
                    "segments": segs,
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        return {
            "path": item.payload["path"],
            "text_file": str(txt),
            "segments_file": str(js),
            "language": getattr(info, "language", None),
            "duration_s": round(float(getattr(info, "duration", 0) or 0), 1),
        }

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        out_dir = ctx.allowed(spec.output, what="output target")
        index = Path(out_dir) / "index.json"
        ok = [r for _, r in results if "skipped" not in r]
        atomic_write_text(
            index,
            json.dumps(
                {
                    "job": self.name,
                    "task_id": ctx.task_id,
                    "model": spec.model,
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "items": [r for _, r in results],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        return Output(
            paths=[str(out_dir)], summary=f"{len(ok)} transcribed, {len(results) - len(ok)} skipped"
        )


register(Transcribe())
