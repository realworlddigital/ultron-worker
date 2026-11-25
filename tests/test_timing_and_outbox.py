"""Per-item phase timing, job-thread backbone writes that never wait on the network, and the
desktop shell's wake lock."""

from __future__ import annotations

import json
import logging
import re
import threading
import time

import pytest

from ultron_worker.backbone import WRITE_TIMEOUT, BackboneError, BackboneUnavailable
from ultron_worker.desktop.autostart import Autostart
from ultron_worker.desktop.controller import App
from ultron_worker.desktop.layout import Install
from ultron_worker.desktop.wakelock import HOLD, RELEASE, WakeLock
from ultron_worker.jobs.vision import VisionTag
from ultron_worker.runner import Outbox, _Event, _last_item, _last_item_text

from .conftest import PNG, FakeBackbone, FakeOllama
from .test_log_and_lock import write_config


class SlowBackbone(FakeBackbone):
    """Every write takes ``delay`` seconds (a flapping tunnel's read timeout, scaled down)."""

    delay = 0.3

    def _send(self, method, path, params, body, *, timeout=30.0):  # type: ignore[no-untyped-def]
        if method != "GET":
            time.sleep(self.delay)
        return super()._send(method, path, params, body, timeout=timeout)


class RepairOllama(FakeOllama):
    """The first reply per item won't parse, so every item needs a repair re-ask."""

    def generate(self, model, prompt, **kw):  # type: ignore[no-untyped-def]
        reply = super().generate(model, prompt, **kw)
        return "not json" if len(self.calls) % 2 else reply


def test_one_timing_line_per_item_with_every_phase(make, tmp_path, monkeypatch, caplog) -> None:
    lib = tmp_path / "Guild Hall"
    lib.mkdir()
    (lib / "C8562.MP4").write_bytes(b"\0" * 16)
    (lib / "a.png").write_bytes(PNG)

    def fake_frames(self, path, scratch, n, ctx):  # type: ignore[no-untyped-def]
        time.sleep(0.05)
        scratch.mkdir(parents=True, exist_ok=True)
        frame = scratch / "frame00.png"
        frame.write_bytes(PNG)
        return [frame]

    monkeypatch.setattr(VisionTag, "_frames", fake_frames)
    runner, bb, ollama = make(ollama=RepairOllama())
    bb.add("t1", inputs=[str(lib)], output=str(tmp_path / "tags.jsonl"))
    with caplog.at_level(logging.INFO, logger="ultron"):
        runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("item ")]
    assert len(lines) == 2
    video = next(ln for ln in lines if "C8562.MP4" in ln)
    root = tmp_path.name
    assert video.startswith(f"item {root}/Guild Hall/C8562.MP4 done in ")
    for phase in ("stat ", "frames ", "model ", "other ", "post "):
        assert phase in video, video
    assert "model " in video and " x2" in video  # the repair re-ask is counted
    assert any("enumerated in" in r.getMessage() for r in caplog.records)
    key = str((lib / "C8562.MP4").resolve())
    timing = runner.store.item_timing("t1", key)
    assert timing is not None and timing["frames"] >= 0.05
    assert {"stat", "frames", "model", "other", "total"} <= set(timing)
    assert timing["total"] >= timing["frames"] + timing["model"]
    record = next(
        json.loads(x) for x in (tmp_path / "tags.jsonl").read_text().splitlines() if "MP4" in x
    )
    assert "timing" not in record  # the output index stays as it was


def test_slow_backbone_does_not_delay_the_next_item(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    slow = SlowBackbone(runner.cfg)
    runner.bb = slow
    slow.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))
    stamps: list[float] = []
    sends_at_call: list[int] = []

    def hook(n: int) -> None:
        stamps.append(time.monotonic())
        sends_at_call.append(len(slow.sent))

    ollama.hook = hook
    runner.tick()
    assert slow.tasks["t1"]["status"] == "done"
    assert len(stamps) == 5
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert max(gaps) < SlowBackbone.delay / 2, gaps  # no event write between items
    assert len(set(sends_at_call)) == 1  # not one backbone call during the item loop
    acts = slow.actions("t1")
    assert acts[0] == "ultron.claimed" and acts[-1] == "ultron.completed"
    assert acts.count("ultron.progress") >= 5  # queued, then delivered in order
    writes = [t for (m, _), t in zip(slow.sent, slow.timeouts, strict=True) if m != "GET"]
    assert writes and max(writes) <= WRITE_TIMEOUT <= 10


def test_dead_backbone_mid_job_costs_no_item_time_then_flushes(make, library, tmp_path) -> None:
    runner, bb, ollama = make()
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    sends: list[int] = []

    def hook(n: int) -> None:
        if n == 1:
            bb.down = True
        sends.append(len(bb.sent))

    ollama.hook = hook
    runner.tick()
    assert runner.store.job("t1")["status"] == "finished" and out.exists()
    assert sends[0] == sends[-1]  # the job thread made no backbone call while the tunnel was dead
    assert len(runner.outbox) >= 2  # claimed + progress kept for later
    bb.down = False
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done" and len(runner.outbox) == 0
    acts = bb.actions("t1")
    assert acts[0] == "ultron.claimed" and acts[-1] == "ultron.completed"
    assert "ultron.progress" in acts


def test_outbox_coalesces_bounds_and_keeps_order() -> None:
    box = Outbox(limit=3)
    for i in range(4):
        box.put(_Event("progress", "t1", f"p{i}", data={"done": i}), coalesce=True)
    box.put(_Event("progress", "t2", "other card"), coalesce=True)
    assert len(box) == 2  # newest progress per card
    for i in range(3):
        box.put(_Event("retry", "t1", f"r{i}"))
    assert len(box) == 3 and box.dropped == 2
    sent: list[str] = []
    calls = {"n": 0}

    def send(e: _Event) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise BackboneError(422, "refused")  # dropped, not retried
        if calls["n"] == 2:
            raise BackboneUnavailable("530")  # stop, keep the rest
        sent.append(e.summary)

    with pytest.raises(BackboneUnavailable):
        box.flush(send)
    assert len(box) == 2
    assert box.flush(send) == 2 and sent == ["r1", "r2"] and len(box) == 0


def test_wake_lock_sends_only_changes() -> None:
    flags: list[int] = []
    lock = WakeLock(lambda f: flags.append(f) or 1)
    assert lock.supported
    assert lock.hold(True) and lock.hold(True)
    assert lock.hold(False) is False
    lock.release()
    assert flags == [HOLD, RELEASE]
    refused = WakeLock(lambda f: 0)
    assert refused.hold(True) is False
    assert WakeLock(auto=False).hold(True) is False  # unsupported: a no-op


def test_app_holds_the_wake_lock_while_a_job_runs(tmp_path) -> None:
    write_config(tmp_path, tmp_path / "state")
    flags: list[int] = []
    app = App(
        Install(tmp_path, config=tmp_path / "config.toml"),
        autostart=Autostart(["x"], None),
        wake_lock=WakeLock(lambda f: flags.append(f) or 1),
    )
    state: dict = {"current": {"task_id": "t1"}}
    app.runner_state = lambda: state  # type: ignore[method-assign]
    assert app.keep_awake() is True and app.status()["runner"]["wake_lock"] is True
    state = {"current": None}
    assert app.keep_awake() is False
    app.runner_state = lambda: None  # type: ignore[method-assign]
    assert app.keep_awake() is False  # runner gone: never hold
    state = {"current": {"task_id": "t2"}}
    app.runner_state = lambda: state  # type: ignore[method-assign]
    app.keep_awake()
    app._stop.set()
    t = threading.Thread(target=app._loop)
    t.start()
    t.join(5)
    assert flags == [HOLD, RELEASE, HOLD, RELEASE]  # the monitor loop releases on exit
    assert not app.wake.held


def test_wake_lock_is_a_no_op_off_windows() -> None:
    import sys

    if sys.platform != "win32":
        assert WakeLock().supported is False


def test_item_timing_counts_calls() -> None:
    from ultron_worker.jobs.base import ItemTiming

    t = ItemTiming()
    with t.phase("model"):
        pass
    with t.phase("model"):
        pass
    assert t.calls == {"model": 2} and t.seconds["model"] >= 0


class FlakyEvents(FakeBackbone):
    """The next ``fail`` event posts hit a 502, then the tunnel is back."""

    fail = 0

    def _send(self, method, path, params, body, *, timeout=30.0):  # type: ignore[no-untyped-def]
        if path == "/events" and self.fail:
            self.fail -= 1
            self.sent.append((method, path))
            self.timeouts.append(timeout)
            raise BackboneUnavailable("backbone 502")
        return super()._send(method, path, params, body, timeout=timeout)


def test_report_waits_for_queued_job_events(make, library, tmp_path, cfg) -> None:
    bb = FlakyEvents(cfg)
    runner, _, ollama = make(bb=bb)
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    ollama.hook = lambda n: setattr(bb, "fail", 1) if n == 1 else None
    runner.tick()
    assert runner.store.job("t1")["status"] == "finished"
    assert "ultron.completed" not in bb.actions("t1")  # report waited for the outbox
    assert bb.tasks["t1"]["status"] == "in_progress"
    runner.tick()
    acts = bb.actions("t1")
    assert len(runner.outbox) == 0 and bb.tasks["t1"]["status"] == "done"
    assert acts[-1] == "ultron.completed" and "ultron.progress" in acts
    assert acts.index("ultron.completed") > max(
        i for i, a in enumerate(acts) if a == "ultron.progress"
    )


def test_cut_short_response_is_unavailable_not_a_crash(cfg, monkeypatch) -> None:
    import http.client
    import urllib.request

    from ultron_worker.backbone import Backbone

    class Truncated:
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc):  # type: ignore[no-untyped-def]
            return False

        def read(self):  # type: ignore[no-untyped-def]
            raise http.client.IncompleteRead(b"{", 10)

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Truncated())
    with pytest.raises(BackboneUnavailable):
        Backbone(cfg).request("GET", "/me")


def test_runner_gui_cut_short_does_not_kill_the_monitor(tmp_path, monkeypatch) -> None:
    import http.client

    from ultron_worker.desktop import controller

    write_config(tmp_path, tmp_path / "state")
    app = App(
        Install(tmp_path, config=tmp_path / "config.toml"),
        autostart=Autostart(["x"], None),
        wake_lock=WakeLock(lambda f: 1),
    )

    class Conn:
        def __init__(self, *a, **k) -> None:  # type: ignore[no-untyped-def]
            pass

        def request(self, *a, **k) -> None:  # type: ignore[no-untyped-def]
            pass

        def getresponse(self):  # type: ignore[no-untyped-def]
            raise http.client.IncompleteRead(b"{", 10)

        def close(self) -> None:
            pass

    monkeypatch.setattr(controller.http.client, "HTTPConnection", Conn)
    assert app.settings.gui_enabled
    assert app.runner_state() is None and app.keep_awake() is False


def test_an_event_that_keeps_failing_is_dropped_and_stops_blocking(make, cfg) -> None:
    from ultron_worker.runner import EVENT_ATTEMPTS

    runner, bb, _ = make()
    runner.outbox.put(_Event("cancelled", "gone", "a card the backbone keeps failing"))
    runner.outbox.put(_Event("progress", "t1", "behind it"))
    sends = {"n": 0}

    def send(e: _Event) -> None:
        if e.tid == "gone":
            sends["n"] += 1
            raise BackboneUnavailable("backbone 500")

    for _ in range(EVENT_ATTEMPTS - 1):
        with pytest.raises(BackboneUnavailable):
            runner.outbox.flush(send)
    assert runner.outbox.pending("t1") == 1
    with pytest.raises(BackboneUnavailable):
        runner.outbox.flush(send)  # the last allowed attempt drops it
    assert runner.outbox.pending("gone") == 0 and runner.outbox.dropped == 1
    assert runner.outbox.flush(send) == 1 and len(runner.outbox) == 0
    assert sends["n"] == EVENT_ATTEMPTS


def test_a_failing_event_gets_one_send_per_good_poll(make, library, tmp_path, cfg) -> None:
    bb = FlakyEvents(cfg)
    runner, _, _ = make(bb=bb)
    bb.add("t1", inputs=[str(library)], output=str(tmp_path / "tags.jsonl"))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    runner.store.update_job("t1", status="finished", report_step=0)
    bb.tasks["t1"]["status"] = "in_progress"
    runner.outbox.put(_Event("progress", "t1", "late"))
    bb.fail = 99
    before = bb.sent.count(("POST", "/events"))
    runner.tick()  # poll flush fails; the report (finished job) must not retry it this poll
    assert bb.sent.count(("POST", "/events")) - before == 1
    assert runner.outbox.pending("t1") == 1 and bb.tasks["t1"]["status"] == "in_progress"


PREFIX = re.compile(r"^Ultron t1: (\d+)/(\d+) \((\d+(?:\.\d+)?)%\)")


def _item_progress(bb: FakeBackbone) -> list[dict]:
    return [e for e in bb.events if e["action"] == "ultron.progress" and e["data"]["current_item"]]


def test_progress_event_carries_the_last_items_phases(make, tmp_path, monkeypatch) -> None:
    lib = tmp_path / "Guild Hall"
    lib.mkdir()
    (lib / "C8563.MP4").write_bytes(b"\0" * 16)

    def fake_frames(self, path, scratch, n, ctx):  # type: ignore[no-untyped-def]
        time.sleep(0.05)
        scratch.mkdir(parents=True, exist_ok=True)
        frame = scratch / "frame00.png"
        frame.write_bytes(PNG)
        return [frame]

    monkeypatch.setattr(VisionTag, "_frames", fake_frames)
    runner, bb, _ = make()
    bb.add("t1", inputs=[str(lib)], output=str(tmp_path / "tags.jsonl"))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    [event] = _item_progress(bb)
    summary = event["summary"]
    assert summary.startswith("Ultron t1: 1/1 (100.0%) - last item ")  # count prefix unchanged
    name = f"{tmp_path.name}/Guild Hall/C8563.MP4"
    assert f"- last item {name}: stat " in summary
    for phase in ("frames ", "model ", "other ", "post "):
        assert f", {phase}" in summary, summary
    assert re.search(r"frames \d+\.\ds", summary)
    last = event["data"]["last_item"]
    assert last["name"] == name and last["outcome"] == "done" and last["error"] is None
    assert {"stat", "frames", "model", "other", "post"} == set(last["phases"])
    assert last["phases"]["frames"] >= 0.05
    timing = runner.store.item_timing("t1", str((lib / "C8563.MP4").resolve()))
    assert timing is not None and last["phases"]["frames"] == timing["frames"]  # same values
    assert last["total_s"] >= timing["total"]
    assert event["data"]["done"] == 1 and event["data"]["total"] == 1  # existing fields kept
    # job start / output-written progress has no last item: plain count, as before
    plain = [e for e in bb.events if e["action"] == "ultron.progress" and e not in [event]]
    assert plain and all(PREFIX.fullmatch(e["summary"]) for e in plain)
    assert all("last_item" not in e["data"] for e in plain)


class GarbageOllama(FakeOllama):
    def generate(self, model, prompt, **kw):  # type: ignore[no-untyped-def]
        super().generate(model, prompt, **kw)
        return "not json"


def test_failed_item_progress_says_so_with_its_error(make, library, tmp_path) -> None:
    runner, bb, _ = make(ollama=GarbageOllama())
    bb.add("t1", inputs=[str(library / "a.png")], output=str(tmp_path / "tags.jsonl"))
    runner.tick()
    [event] = _item_progress(bb)[:1]
    summary = event["summary"]
    assert summary.startswith("Ultron t1: 0/1 (0.0%) - last item ")
    assert re.search(r" failed \(stat \d+\.\ds, image \d+\.\ds, model \d+\.\ds, ", summary), summary
    assert "s): unusable model reply after a repair re-ask" in summary
    last = event["data"]["last_item"]
    assert last["outcome"] == "failed"
    assert last["error"].startswith("unusable model reply after a repair re-ask")
    assert "model" in last["phases"] and "post" in last["phases"]


def test_long_item_error_is_shortened_in_the_summary_only() -> None:
    last = _last_item("x.png", {"skipped": "boom " * 100}, {"stat": 0.01, "total": 0.02}, 0.0)
    text = _last_item_text(last)
    assert text.startswith("x.png skipped (stat 0.0s, post 0.0s): boom boom")
    assert text.endswith("...") and len(text) < 260 and last["error"] == "boom " * 100


def test_kinds_without_phases_keep_the_plain_progress_summary(make, library, tmp_path) -> None:
    runner, bb, _ = make()
    bb.add(
        "t1",
        kind="folder_inventory",
        model=None,
        inputs=[str(library)],
        output=str(tmp_path / "inventory.jsonl"),
    )
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done", bb.events
    events = [e for e in bb.events if e["action"] == "ultron.progress"]
    assert _item_progress(bb)  # it does report per item...
    assert all(PREFIX.fullmatch(e["summary"]) for e in events)  # ...in today's format
    assert all("last_item" not in e["data"] for e in events)
