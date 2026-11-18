"""text_batch: run one prompt over many texts with a local Ollama model.

Each input file is one item, except ``.jsonl`` files, where each line is an item
(``params.id_field`` / ``params.text_field``, default ``id`` / ``text``). ``params.prompt``
holds the template; ``{text}`` is replaced with the item's text.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..ollama import OllamaError, OllamaModelMissing, OllamaUnavailable
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

TEXT_EXTS = {".txt", ".md", ".jsonl", ".json", ".csv", ".html", ".srt", ".vtt"}
MAX_CHARS = 32000


def text_items(spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
    id_field = str(spec.params.get("id_field", "id"))
    text_field = str(spec.params.get("text_field", "text"))
    items: list[WorkItem] = []
    out = ctx.allowed(spec.output, what="output target")
    for f in iter_files(
        spec.inputs,
        ctx,
        TEXT_EXTS,
        recursive=bool(spec.params.get("recursive", True)),
        exclude=(out,),
    ):
        if f.suffix.lower() != ".jsonl":
            items.append(
                WorkItem(key=file_key(f), label=str(f), payload={"path": str(f), "id": f.stem})
            )
            continue
        with f.open(encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    row = {}
                rid = str(row.get(id_field, n)) if isinstance(row, dict) else str(n)
                items.append(
                    WorkItem(
                        key=f"{file_key(f)}#{n}",
                        label=f"{f.name} #{rid}",
                        payload={"path": str(f), "line": n, "id": rid, "text_field": text_field},
                    )
                )
    return items


def item_text(item: WorkItem, spec: JobSpec) -> str:
    path = Path(item.payload["path"])
    max_chars = int(spec.params.get("max_chars", MAX_CHARS))
    try:
        if "line" not in item.payload:
            return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
        with path.open(encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if n == item.payload["line"]:
                    row = json.loads(line)
                    text = row.get(item.payload["text_field"]) if isinstance(row, dict) else None
                    if not isinstance(text, str) or not text.strip():
                        raise ItemFailed(f"line {n} has no {item.payload['text_field']!r} text")
                    return text[:max_chars]
    except (OSError, ValueError) as exc:
        raise ItemFailed(f"unreadable input: {exc}") from exc
    raise ItemFailed("line no longer exists in the input file")


def finalize_records(
    kind: str, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
) -> Output:
    out = ctx.allowed(spec.output, what="output target")
    if out.is_dir():
        raise JobError(f"output target {str(out)!r} is a folder; name a .json/.jsonl file")
    ok = [r for _, r in results if "skipped" not in r]
    write_records(
        out,
        {
            "job": kind,
            "task_id": ctx.task_id,
            "model": spec.model,
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "done": len(ok),
            "skipped": len(results) - len(ok),
        },
        [r for _, r in results],
    )
    return Output(paths=[str(out)], summary=f"{len(ok)} done, {len(results) - len(ok)} skipped")


class TextBatch(JobKind):
    name = "text_batch"

    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        super().validate(spec, ctx)
        if "{text}" not in str(spec.params.get("prompt", "")):
            raise JobError("text_batch needs params.prompt containing {text}")

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        return text_items(spec, ctx)

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        prompt = str(spec.params["prompt"]).replace("{text}", item_text(item, spec))
        try:
            response = ctx.ollama.generate(
                spec.model,
                prompt,
                json_format=bool(spec.params.get("json")),
                options=spec.params.get("options"),
            )
        except (OllamaUnavailable, OllamaModelMissing):
            raise
        except OllamaError as exc:
            raise ItemFailed(str(exc)) from exc
        return {"id": item.payload["id"], "source": item.payload["path"], "response": response}

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        return finalize_records(self.name, spec, results, ctx)


register(TextBatch())
