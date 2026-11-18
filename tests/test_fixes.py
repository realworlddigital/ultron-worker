from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from ultron_worker.jobs.base import JobContext, iter_files

from .conftest import PNG


def ctx_for(tmp_path: Path, root: Path) -> JobContext:
    return JobContext(
        task_id="t", ollama=None, allowed_roots=(root,), work_dir=tmp_path / "w",
        stop=threading.Event(),
    )


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_files_outside_allowed_roots_are_skipped(tmp_path) -> None:
    root, outside = tmp_path / "lib", tmp_path / "secret"
    root.mkdir(), outside.mkdir()
    (root / "a.png").write_bytes(PNG)
    (outside / "b.png").write_bytes(PNG)
    (root / "b.png").symlink_to(outside / "b.png")
    (root / "dir").symlink_to(outside, target_is_directory=True)
    found = iter_files([str(root)], ctx_for(tmp_path, root), {".png"})
    assert found == [(root / "a.png").resolve()]


def test_missing_model_stops_the_job_instead_of_skipping_items(make, library, tmp_path) -> None:
    from ultron_worker.ollama import OllamaModelMissing

    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))

    def missing(n: int) -> None:
        raise OllamaModelMissing("ollama has no model 'llava:13b'")

    ollama.hook = missing
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    assert runner.store.item_results("t1") == {}  # nothing checkpointed as skipped
    assert "ultron.retry" not in bb.actions("t1")
    assert not (tmp_path / "tags.jsonl").exists()


def test_transcript_names_unique_ignoring_case(tmp_path) -> None:
    from ultron_worker.jobs.base import JobSpec
    from ultron_worker.jobs.transcribe import Transcribe

    (tmp_path / "a").mkdir(), (tmp_path / "b").mkdir()
    (tmp_path / "a" / "Talk.wav").write_bytes(b"x")
    (tmp_path / "b" / "talk.wav").write_bytes(b"x")
    spec = JobSpec.from_card(
        {"job": {"kind": "transcribe", "inputs": [str(tmp_path)], "model": "m",
                 "output": str(tmp_path / "out"), "budget_minutes": 5}}
    )
    names = [i.payload["name"].casefold() for i in Transcribe().plan(spec, ctx_for(tmp_path, tmp_path))]
    assert len(names) == 2 and len(set(names)) == 2


def test_changed_model_on_redispatch_starts_over(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))

    def die_at_three(n: int) -> None:
        if n >= 3:
            ollama.down = True

    ollama.hook = die_at_three
    for _ in range(4):
        runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david" and runner.store.item_results("t1")
    ollama.down, ollama.hook = False, None
    ollama.calls.clear()
    bb.tasks["t1"]["data"]["job"]["model"] = "qwen2.5vl:7b"
    bb.tasks["t1"]["status"] = "dispatched"
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    assert len(ollama.calls) == 5 and {c["model"] for c in ollama.calls} == {"qwen2.5vl:7b"}


def test_output_inside_input_folder_is_not_reingested(make, tmp_path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for i in range(3):
        (docs / f"d{i}.txt").write_text(f"doc {i}")
    out = docs / "summaries.jsonl"
    out.write_text('{"id": "old", "text": "from a previous run"}\n' * 4)
    runner, bb, ollama = make()
    bb.add("t1", kind="text_batch", model="qwen2.5:14b", inputs=[str(docs)], output=str(out),
           params={"prompt": "Summarise: {text}"})
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done" and len(ollama.calls) == 3
