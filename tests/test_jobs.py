from __future__ import annotations

import json
import sys
import textwrap

import pytest

from ultron_worker import jobs
from ultron_worker.jobs.base import JobError, JobSpec
from ultron_worker.jobs.vision import parse_tags


def test_registry_has_v1_kinds_and_no_media_generation() -> None:
    jobs.load(())
    assert set(jobs.kinds()) >= {"vision_tag", "text_batch", "embeddings", "transcribe"}
    assert not any("comfy" in k for k in jobs.kinds())


def test_new_kind_plugs_in_without_touching_the_core(make, tmp_path, monkeypatch) -> None:
    (tmp_path / "my_kinds.py").write_text(
        textwrap.dedent("""
        from ultron_worker.jobs import JobKind, Output, WorkItem, register

        class Count(JobKind):
            name = "count_files"
            def plan(self, spec, ctx):
                return [WorkItem(key=str(i), label=f"n{i}", payload={}) for i in range(3)]
            def run_item(self, item, spec, ctx):
                return {"n": item.key}
            def finalize(self, spec, results, ctx):
                return Output(paths=[], summary=f"{len(results)} counted")

        register(Count())
    """)
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    from dataclasses import replace

    runner, bb, _ = make()
    runner2, _, _ = make(bb=bb, config=replace(runner.cfg, job_modules=("my_kinds",)))
    bb.add("t1", kind="count_files", inputs=[str(tmp_path)], output=str(tmp_path / "o"))
    runner2.tick()
    assert bb.tasks["t1"]["status"] == "done", bb.notes
    assert "3 counted" in bb.notes[0][1]["body"]
    sys.modules.pop("my_kinds", None)


def test_text_batch(make, tmp_path) -> None:
    src = tmp_path / "in.jsonl"
    src.write_text('{"id": "a", "text": "one"}\n{"id": "b", "text": "two"}\n')
    runner, bb, ollama = make()
    bb.add(
        "t1",
        kind="text_batch",
        model="qwen2.5:14b",
        inputs=[str(src)],
        output=str(tmp_path / "out.jsonl"),
        params={"prompt": "Summarise: {text}"},
    )
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    rows = [json.loads(x) for x in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["a", "b"] and rows[0]["response"].startswith("answer")
    assert ollama.calls[0]["prompt"] == "Summarise: one"


def test_text_batch_needs_prompt(make, tmp_path) -> None:
    runner, bb, _ = make()
    bb.add("t1", kind="text_batch", inputs=[str(tmp_path)], output=str(tmp_path / "o.jsonl"))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"


def test_embeddings(make, tmp_path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("hello")
    (tmp_path / "docs" / "b.txt").write_text("world!")
    runner, bb, _ = make()
    bb.add(
        "t1",
        kind="embeddings",
        model="nomic-embed-text",
        inputs=[str(tmp_path / "docs")],
        output=str(tmp_path / "emb.jsonl"),
    )
    runner.tick()
    rows = [json.loads(x) for x in (tmp_path / "emb.jsonl").read_text().splitlines()]
    assert len(rows) == 2 and all(r["dims"] == 3 for r in rows)


def test_transcribe_without_faster_whisper_surfaces(make, tmp_path, monkeypatch) -> None:
    import ultron_worker.jobs.transcribe as tr

    monkeypatch.setattr(tr, "available", lambda: False)
    (tmp_path / "a.mp3").write_bytes(b"x")
    runner, bb, _ = make()
    bb.add(
        "t1",
        kind="transcribe",
        model="large-v3",
        inputs=[str(tmp_path)],
        output=str(tmp_path / "tx"),
    )
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    assert "faster-whisper" in bb.notes[0][1]["body"]


def test_job_spec_validation() -> None:
    with pytest.raises(JobError, match="budget_minutes"):
        JobSpec.from_card({"job": {"kind": "k", "model": "m", "output": "o", "inputs": ["i"]}})
    spec = JobSpec.from_card(
        {"job": {"kind": "k", "model": "m", "output": "o", "inputs": "i", "budget_minutes": 480}}
    )
    assert spec.inputs == ("i",) and spec.budget_minutes == 480


def test_parse_tags_tolerates_model_noise() -> None:
    cap, tags = parse_tags('Sure! {"caption": "A dog", "tags": ["Dog", "dog", "#park"]}', 10)
    assert cap == "A dog" and tags == ["dog", "park"]
