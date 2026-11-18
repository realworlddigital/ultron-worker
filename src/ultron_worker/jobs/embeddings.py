"""embeddings: embed many texts with a local Ollama embedding model (same inputs as
text_batch). Use a ``.jsonl`` output target for big runs."""

from __future__ import annotations

from typing import Any

from ..ollama import OllamaError, OllamaModelMissing, OllamaUnavailable
from . import register
from .base import ItemFailed, JobContext, JobKind, JobSpec, Output, WorkItem
from .text import finalize_records, item_text, text_items


class Embeddings(JobKind):
    name = "embeddings"

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        return text_items(spec, ctx)

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        try:
            [vector] = ctx.ollama.embed(spec.model, [item_text(item, spec)])
        except (OllamaUnavailable, OllamaModelMissing):
            raise
        except OllamaError as exc:
            raise ItemFailed(str(exc)) from exc
        return {
            "id": item.payload["id"],
            "source": item.payload["path"],
            "dims": len(vector),
            "embedding": vector,
        }

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        return finalize_records(self.name, spec, results, ctx)


register(Embeddings())
