"""vision_tag frame extraction: the video is copied to the item's scratch dir before any
probe/seek, and a frame call that times out is logged and counted, never swallowed."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from ultron_worker.jobs import vision
from ultron_worker.jobs.base import SCRATCH_DIR, JobContext, JobStopped

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell stubs")

FFPROBE = """#!/bin/sh
for a; do last=$a; done
state=probe; [ -f "$last" ] || state=probe-missing
echo "$state $last" >> "$STUB_LOG"
echo 10.0
"""
FFMPEG = """#!/bin/sh
ss=""; in=""; prev=""
for a; do
  [ "$prev" = "-ss" ] && ss=$a
  [ "$prev" = "-i" ] && in=$a
  prev=$a; last=$a
done
echo "ffmpeg $ss $in" >> "$STUB_LOG"
case " $STUB_HANG " in *" $ss "*) exec sleep 5;; esac
printf 'jpg' > "$last"
"""


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """Stub ffprobe (10 s video) / ffmpeg on PATH; ``STUB_HANG`` seek offsets sleep 5 s."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("ffprobe", FFPROBE), ("ffmpeg", FFMPEG)):
        script = bin_dir / name
        script.write_text(body)
        script.chmod(0o755)
    log = tmp_path / "stub.log"
    log.touch()
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("STUB_HANG", "")
    monkeypatch.setattr(vision, "FRAME_TIMEOUT", 0.5)
    return log


def _share_video(tmp_path: Path, name: str = "C9001.MP4") -> Path:
    share = tmp_path / "NAS" / "Natick"
    share.mkdir(parents=True)
    video = share / name
    video.write_bytes(b"\0" * 4096)
    return video


def test_share_video_is_copied_to_scratch_before_probing(make, tmp_path, stubs) -> None:
    video = _share_video(tmp_path)
    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(video.parent)], output=str(tmp_path / "tags.jsonl"))
    runner.tick()
    assert bb.tasks["t1"]["status"] == "done"
    calls = stubs.read_text().splitlines()
    probes = [c for c in calls if c.startswith("probe")]
    seeks = [c.split(" ", 2)[2] for c in calls if c.startswith("ffmpeg")]
    assert len(probes) == 1 and probes[0].startswith("probe ")  # the copy existed when probed
    probed = Path(probes[0].split(" ", 1)[1])
    assert probed != video and SCRATCH_DIR in probed.parts and probed.suffix == ".mp4"
    assert len(seeks) == 4 and set(seeks) == {str(probed)}  # every seek reads the local copy
    assert not probed.exists()  # dies with the scratch dir
    assert len(ollama.calls[0]["images"]) == 4
    lines = (tmp_path / "tags.jsonl").read_text().splitlines()
    [record] = [json.loads(x) for x in lines if "C9001.MP4" in x]
    assert record["frames"] == 4 and record["frame_timeouts"] == 0


def test_frame_timeout_is_logged_and_counted(make, tmp_path, stubs, monkeypatch, caplog) -> None:
    monkeypatch.setenv("STUB_HANG", "2.00")  # frame 0 of 4 (at 2.00 s of 10 s) hangs
    video = _share_video(tmp_path)
    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(video.parent)], output=str(tmp_path / "tags.jsonl"))
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="ultron"):
        runner.tick()
    assert time.monotonic() - started < 4  # killed at FRAME_TIMEOUT, not left to sleep
    assert bb.tasks["t1"]["status"] == "done"
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    msg = warning.getMessage()
    assert str(video) in msg and "frame 0/4" in msg and "timed out after 0.5s" in msg
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("item ")]
    assert " done in " in line and "frame_timeouts 1" in line
    for phase in ("stat ", "frames ", "model ", "other ", "post "):
        assert phase in line, line
    key = str(video.resolve())
    checkpoint = runner.store.item_results("t1")[key]
    assert checkpoint["frames"] == 3 and checkpoint["frame_timeouts"] == 1
    assert len(ollama.calls[0]["images"]) == 3
    [event] = [
        e for e in bb.events if e["action"] == "ultron.progress" and e["data"]["current_item"]
    ]
    assert event["data"]["last_item"]["counts"] == {"frame_timeouts": 1}
    assert "frame_timeouts 1" in event["summary"]


def test_every_frame_timing_out_still_fails_the_item(make, tmp_path, stubs, monkeypatch, caplog):
    monkeypatch.setenv("STUB_HANG", "2.00 4.00 6.00 8.00")
    video = _share_video(tmp_path)
    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(video.parent)], output=str(tmp_path / "tags.jsonl"))
    with caplog.at_level(logging.INFO, logger="ultron"):
        runner.tick()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("timed out" in w for w in warnings) == 4
    result = runner.store.item_results("t1")[str(video.resolve())]
    assert "4 of 4 frame calls timed out" in result["skipped"]
    assert not ollama.calls
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("item ")]
    assert " skipped in " in line and "frame_timeouts 4" in line


def test_stalled_scratch_copy_is_bounded_and_fails_the_item(
    make, tmp_path, stubs, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(vision, "COPY_SCRIPT", "import time; time.sleep(5)")  # share read hangs
    video = _share_video(tmp_path)
    runner, bb, ollama = make()
    bb.add("t1", inputs=[str(video.parent)], output=str(tmp_path / "tags.jsonl"))
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="ultron"):
        runner.tick()
    assert time.monotonic() - started < 4  # the job thread is not held by the stalled read
    result = runner.store.item_results("t1")[str(video.resolve())]
    assert "copy to scratch timed out" in result["skipped"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(video) in w and "copy to scratch" in w for w in warnings)
    assert not stubs.read_text() and not ollama.calls  # never probed or seeked


def test_runner_stop_kills_a_stalled_scratch_copy(tmp_path, stubs, monkeypatch) -> None:
    pid_file = tmp_path / "copy.pid"
    script = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(30)"
    )
    monkeypatch.setattr(vision, "COPY_SCRIPT", script)
    monkeypatch.setattr(vision, "COPY_MIN_BYTES_PER_S", 1)  # deadline far beyond the stop
    video = _share_video(tmp_path)
    ctx = JobContext(task_id="t1", ollama=None, allowed_roots=(tmp_path,), work_dir=tmp_path)
    timer = threading.Timer(1.0, ctx.stop.set)
    timer.start()
    started = time.monotonic()
    with pytest.raises(JobStopped):
        vision.VisionTag()._copy(video, tmp_path / "copy.mp4", ctx)
    assert time.monotonic() - started < 4
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed and reaped, not left running past the stop
