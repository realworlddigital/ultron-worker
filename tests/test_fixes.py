from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from ultron_worker.jobs.base import JobContext, iter_files

from .conftest import PNG


def ctx_for(tmp_path: Path, root: Path) -> JobContext:
    return JobContext(
        task_id="t",
        ollama=None,
        allowed_roots=(root,),
        work_dir=tmp_path / "w",
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
        {
            "job": {
                "kind": "transcribe",
                "inputs": [str(tmp_path)],
                "model": "m",
                "output": str(tmp_path / "out"),
                "budget_minutes": 5,
            }
        }
    )
    names = [
        i.payload["name"].casefold() for i in Transcribe().plan(spec, ctx_for(tmp_path, tmp_path))
    ]
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
    bb.add(
        "t1",
        kind="text_batch",
        model="qwen2.5:14b",
        inputs=[str(docs)],
        output=str(out),
        params={"prompt": "Summarise: {text}"},
    )
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done" and len(ollama.calls) == 3


def test_active_clock_corrects_a_late_sleep_report(monkeypatch) -> None:
    from ultron_worker import runner as mod

    class Board:
        sleep_seconds_total = 0.0

    now = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: now[0])
    board = Board()
    clock = mod._ActiveClock(board)
    now[0] += 3600  # an item spanned a one-hour suspend the watch hasn't reported yet
    first = clock.lap()
    board.sleep_seconds_total = 3590.0
    now[0] += 5
    assert first + clock.lap() == pytest.approx(15.0)


def test_failed_attempt_still_counts_its_active_time(make, library, tmp_path) -> None:
    import time

    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))

    def slow_then_down(n: int) -> None:
        time.sleep(0.05)
        if n == 2:
            ollama.down = True

    ollama.hook = slow_then_down
    runner.tick()
    job = runner.store.job("t1")
    assert job["status"] == "retry_wait" and job["active_seconds"] >= 0.1


def test_shutdown_inside_an_item_is_a_pause_not_an_attempt(make, library, tmp_path) -> None:
    from ultron_worker.jobs.base import JobStopped

    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))

    def stop_at_three(n: int) -> None:
        if n == 3:
            runner.stop.set()
            raise JobStopped

    ollama.hook = stop_at_three
    runner.tick()
    job = runner.store.job("t1")
    assert job["attempts"] == 0 and job["status"] == "running" and job["done"] == 2
    assert bb.tasks["t1"]["status"] == "in_progress"
    assert not {"ultron.retry", "ultron.failed"} & set(bb.actions("t1"))


def test_report_cut_off_midway_never_posts_twice(make, library, tmp_path) -> None:
    from ultron_worker.backbone import BackboneUnavailable

    runner, bb, _ = make()
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))
    real_note, real_transition = bb.note, bb.transition
    fail = {"note": 1, "transition": 1}

    def flaky(name, real):
        def call(*a, **kw):
            if name == "transition" and a[1] != "done":
                return real(*a, **kw)  # the claim
            if fail[name]:
                fail[name] -= 1
                raise BackboneUnavailable("connection reset")
            return real(*a, **kw)

        return call

    bb.note, bb.transition = flaky("note", real_note), flaky("transition", real_transition)
    for _ in range(3):
        runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    assert bb.actions("t1").count("ultron.completed") == 1
    assert len([n for t, n in bb.notes if t == "t1"]) == 1


def test_state_db_from_older_runner_gains_new_columns(tmp_path) -> None:
    import sqlite3

    from ultron_worker.state import Store

    path = tmp_path / "state.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE jobs (task_id TEXT PRIMARY KEY, title TEXT, kind TEXT, model TEXT,"
        " output TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,"
        " done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,"
        " current_item TEXT, active_seconds REAL NOT NULL DEFAULT 0, started_at TEXT,"
        " finished_at TEXT, updated_at TEXT, error TEXT, outputs TEXT, summary TEXT)"
    )
    old.commit()
    old.close()
    store = Store(path)
    job = store.start_job(
        "t1", title="x", kind="text_batch", model="m", output="o", spec="{}", fresh_dispatch=True
    )
    assert job["report_step"] == 0


def test_adding_empty_params_keeps_checkpoints() -> None:
    from ultron_worker.runner import job_fingerprint

    base = {"kind": "vision_tag", "model": "llava"}
    assert job_fingerprint(base) == job_fingerprint({**base, "params": {}})
    assert job_fingerprint(base) == job_fingerprint({**base, "params": None})
    assert job_fingerprint(base) != job_fingerprint({**base, "params": {"frames": 4}})
