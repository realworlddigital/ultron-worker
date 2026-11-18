from __future__ import annotations

import json
from pathlib import Path

from ultron_worker.runner import Runner

from .conftest import FakeBackbone


def vision_card(bb: FakeBackbone, tid: str, library: Path, out: Path, **kw):
    return bb.add(tid, inputs=[str(library)], output=str(out), **kw)


def test_vision_card_picked_up_in_one_poll_and_closed(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    out = tmp_path / "out" / "tags.jsonl"
    vision_card(bb, "t1", library, out)
    runner.tick()  # one poll: claim, run, report
    assert bb.transitions == [("t1", "in_progress"), ("t1", "done")]
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert sorted(Path(r["path"]).name for r in lines) == [
        "a.png",
        "b.png",
        "c.png",
        "d.png",
        "e.png",
    ]
    assert lines[0]["tags"] == ["a", "red", "cat"]  # normalised, de-duplicated
    acts = bb.actions("t1")
    assert acts[0] == "ultron.claimed" and acts[-1] == "ultron.completed"
    assert acts.count("ultron.progress") >= 5
    done = [e for e in bb.events if e["action"] == "ultron.completed"][0]
    assert done["data"]["outputs"] == [str(out)] and done["domain"] == "compute"
    [(tid, note)] = bb.notes
    assert tid == "t1" and str(out) in note["body"] and "5 tagged, 0 skipped" in note["body"]
    assert len(ollama.calls) == 5
    snap = runner.board.snapshot()
    assert snap["current"] is None and snap["recent"][0]["outputs"] == [str(out)]
    assert snap["recent"][0]["reported"] is True


def test_progress_events_carry_counts_and_current_file(make, library, tmp_path) -> None:
    runner, bb, _ = make()
    vision_card(bb, "t1", library, tmp_path / "tags.json")
    runner.tick()
    prog = [e["data"] for e in bb.events if e["action"] == "ultron.progress"]
    assert prog[0]["done"] == 0 and prog[-1]["percent"] == 100.0
    assert any(p["current_item"] and p["current_item"].endswith(".png") for p in prog)
    doc = json.loads((tmp_path / "tags.json").read_text())
    assert doc["tagged"] == 5 and len(doc["items"]) == 5


def test_kill_and_restart_resumes_without_duplicates(make, library, tmp_path) -> None:
    out = tmp_path / "tags.jsonl"
    runner, bb, ollama = make()
    vision_card(bb, "t1", library, out)

    def kill_after_two(n: int) -> None:
        if n == 2:
            runner.stop.set()  # what SIGTERM / sleep-shutdown does

    ollama.hook = kill_after_two
    runner.tick()
    assert bb.tasks["t1"]["status"] == "in_progress" and not out.exists()
    assert runner.store.job("t1")["done"] == 2
    runner.store.close()

    ollama.hook = None
    restarted, _, _ = make(bb=bb, ollama=ollama)  # new process, same state dir
    restarted.tick()
    assert bb.tasks["t1"]["status"] == "done"
    paths = [json.loads(x)["path"] for x in out.read_text().splitlines()]
    assert len(paths) == len(set(paths)) == 5
    tagged = [c["images"][0] for c in ollama.calls]
    assert len(tagged) == len(set(tagged)) == 5  # no image sent to the model twice
    assert "ultron.resumed" in bb.actions("t1")
    resumed = [e for e in bb.events if e["action"] == "ultron.resumed"][0]
    assert resumed["data"]["items_done"] == 2


def test_card_filed_while_offline_waits_then_runs(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    bb.down = True
    vision_card(bb, "t1", library, tmp_path / "tags.jsonl")
    runner.tick()
    snap = runner.board.snapshot()
    assert snap["runner"]["connection"] == "offline" and bb.tasks["t1"]["status"] == "dispatched"
    runner.board.slept(3600)  # Ultron slept
    assert runner.board.activity() == "paused-by-sleep"
    bb.down = False
    runner.tick()  # first poll after wake
    assert bb.tasks["t1"]["status"] == "done" and len(ollama.calls) == 5
    assert runner.board.snapshot()["runner"]["activity"] == "idle"


def test_queue_is_visible_in_filing_order(make, library, tmp_path) -> None:
    runner, bb, _ = make(background=True)
    for tid in ("q1", "q2", "q3"):
        vision_card(bb, tid, library, tmp_path / f"{tid}.jsonl")
    started = []
    runner._execute_safely = lambda card, fresh: started.append(card["id"])  # type: ignore
    runner.tick()
    assert started == ["q1"]
    assert [q["task_id"] for q in runner.board.snapshot()["queue"]] == ["q1", "q2", "q3"]
    runner.board.set_current({"task_id": "q1", "done": 0, "total": 1})
    runner.tick()
    assert [q["task_id"] for q in runner.board.snapshot()["queue"]] == ["q2", "q3"]


def test_job_finishes_while_backbone_down_then_reports(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    out = tmp_path / "tags.jsonl"
    vision_card(bb, "t1", library, out)

    def drop_network(n: int) -> None:
        if n == 3:
            bb.down = True

    ollama.hook = drop_network
    runner.tick()  # the job doesn't need the network: it finishes locally
    assert out.exists() and runner.store.job("t1")["status"] == "finished"
    assert bb.tasks["t1"]["status"] == "in_progress"
    bb.down = False
    runner.tick()  # report only, no rerun
    assert bb.tasks["t1"]["status"] == "done" and len(ollama.calls) == 5
    assert bb.actions("t1").count("ultron.completed") == 1


def test_bounded_retries_then_needs_david(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    ollama.down = True
    vision_card(bb, "t1", library, tmp_path / "tags.jsonl")
    for _ in range(6):
        runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    assert bb.actions("t1").count("ultron.retry") == 2  # attempts 1, 2 retried; 3 surfaced
    assert bb.actions("t1").count("ultron.failed") == 1
    [(_, note)] = bb.notes
    assert note["kind"] == "action" and "3 attempt(s)" in note["body"]
    assert "ollama not reachable" in note["body"]
    [attn] = runner.board.snapshot()["attention"]
    assert attn["status"] == "needs_david" and "ollama" in attn["error"]
    n = len(bb.sent)
    runner.tick()
    assert ("POST", "/tasks/t1/transition") not in bb.sent[n:]  # never retried forever


def test_redispatch_after_needs_david_resumes_from_checkpoint(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    vision_card(bb, "t1", library, tmp_path / "tags.jsonl")

    def die_at_three(n: int) -> None:
        if n >= 3:
            ollama.down = True

    ollama.hook = die_at_three
    for _ in range(4):
        runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    ollama.down, ollama.hook = False, None
    bb.tasks["t1"]["status"] = "dispatched"  # David re-dispatches
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    names = [Path(c["images"][0]).name for c in ollama.calls]
    assert len(set(names)) == 5


def test_bad_spec_and_unknown_kind_go_straight_to_needs_david(make, tmp_path) -> None:
    runner, bb, _ = make()
    bb.add("t1", kind="comfyui_render", inputs=[str(tmp_path)], output=str(tmp_path / "o.json"))
    bb.add("t2", inputs=[str(tmp_path)])  # no output
    runner.tick()
    runner.tick()
    assert bb.tasks["t1"]["status"] == bb.tasks["t2"]["status"] == "needs_david"
    errs = {
        e["target"]["id"]: e["data"]["error"] for e in bb.events if e["action"] == "ultron.failed"
    }
    assert "unknown job kind 'comfyui_render'" in errs["t1"]
    assert "missing output" in errs["t2"]
    assert "ultron.retry" not in bb.actions()


def test_paths_outside_allowed_roots_are_refused(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    bb.add("t1", inputs=["/etc"], output=str(tmp_path / "o.jsonl"))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david" and ollama.calls == []
    assert "outside runner.allowed_roots" in bb.notes[0][1]["body"]


def test_budget_is_per_card_and_checkpointed(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    vision_card(bb, "t1", library, tmp_path / "tags.jsonl", budget_minutes=0.0001)

    def slow(n: int) -> None:
        import time

        time.sleep(0.02)

    ollama.hook = slow
    runner.tick()
    assert bb.tasks["t1"]["status"] == "needs_david"
    assert "budget" in bb.notes[0][1]["body"]
    assert runner.store.job("t1")["done"] >= 1


def test_identity_mismatch_refuses_to_work(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    bb.me_actor = "gilfoyle"
    vision_card(bb, "t1", library, tmp_path / "tags.jsonl")
    runner.tick()
    assert bb.tasks["t1"]["status"] == "dispatched" and ollama.calls == []
    assert "not 'ultron-runner'" in runner.board.snapshot()["runner"]["connection_detail"]


def test_heartbeat_reports_activity(make, library, tmp_path) -> None:
    runner, bb, _ = make()
    runner.heartbeat()
    assert bb.statuses[-1]["state"] == "idle" and bb.statuses[-1]["host"] == "ultron-test"
    bb.down = True
    runner.heartbeat()
    assert runner.board.snapshot()["runner"]["connection"] == "offline"


def test_run_forever_headless_background(make, library, tmp_path) -> None:
    import threading
    import time

    runner, bb, ollama = make(background=True)
    out = tmp_path / "tags.jsonl"
    vision_card(bb, "t1", library, out)
    t = threading.Thread(target=runner.run_forever, daemon=True)
    t.start()
    deadline = time.time() + 10
    while bb.tasks["t1"]["status"] != "done" and time.time() < deadline:
        time.sleep(0.05)
    runner.stop.set()
    runner.poll_now.set()
    t.join(timeout=10)
    assert bb.tasks["t1"]["status"] == "done" and out.exists()
    assert isinstance(runner, Runner) and len(ollama.calls) == 5
