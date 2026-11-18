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
