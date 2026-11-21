from __future__ import annotations

import sys

from ultron_worker.desktop.health import parse_check, run_check

GOOD = """backbone: OK, key is actor 'ultron-runner'
queue: 3 dispatched card(s)
ollama: OK
job kinds: embeddings, text_batch, transcribe, vision_tag; faster-whisper not installed
"""


def test_all_green() -> None:
    lights = parse_check(0, GOOD)
    assert {k: v.state for k, v in lights.items()} == {
        "backbone": "ok",
        "queue": "ok",
        "ollama": "ok",
    }
    assert lights["backbone"].label == "ultron-runner"
    assert lights["queue"].label == "3 dispatched"


def test_backbone_failed_takes_queue_with_it() -> None:
    out = "backbone: FAILED 401 Unauthorized\nollama: OK\njob kinds: vision_tag\n"
    lights = parse_check(1, out)
    assert lights["backbone"].state == "bad" and "401" in lights["backbone"].detail
    assert lights["queue"].state == "bad"
    assert lights["ollama"].state == "ok"


def test_ollama_failed() -> None:
    out = GOOD.replace("ollama: OK", "ollama: FAILED connection refused")
    lights = parse_check(1, out)
    assert lights["ollama"].state == "bad" and lights["ollama"].detail == "connection refused"
    assert lights["backbone"].state == "ok"


def test_config_error_turns_everything_red() -> None:
    lights = parse_check(2, "config: no backbone key: set ULTRON_BACKBONE_KEY or backbone.key_file")
    assert all(v.state == "bad" and "no backbone key" in v.detail for v in lights.values())


def test_nonzero_exit_without_a_reason_is_not_green() -> None:
    lights = parse_check(1, "Traceback (most recent call last):\nRuntimeError: boom")
    assert all(v.state == "bad" for v in lights.values())


def test_run_check_filters_log_lines() -> None:
    script = (
        "import sys; print('2026-11-20 INFO ultron ultron-runner 0.1.0 as x'); "
        "print('backbone: OK, key is actor \\'x\\''); print('queue: 0 dispatched card(s)'); "
        "print('ollama: OK'); sys.exit(0)"
    )
    report = run_check([sys.executable, "-c", script, "--"])
    assert report.ok and report.lights["queue"].label == "0 dispatched"
    assert "INFO" not in report.output


def test_run_check_missing_executable() -> None:
    report = run_check(["/nonexistent/python"])
    assert not report.ok and all(v.state == "bad" for v in report.lights.values())
