from __future__ import annotations

import http.client
import json
import threading
import time

import pytest

from ultron_worker.gui import StatusGui


def get(gui: StatusGui, path: str, method: str = "GET", host: str | None = None):
    h, p = gui.server.server_address[:2]
    conn = http.client.HTTPConnection(h, p, timeout=5)
    conn.request(method, path, headers={"Host": host or f"127.0.0.1:{p}"})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp, body


@pytest.fixture
def gui(make):
    runner, bb, ollama = make()
    g = StatusGui(runner.board, host="127.0.0.1", port=0).start()
    yield g, runner, bb, ollama
    g.close()


def test_binds_localhost_only(make) -> None:
    runner, _, _ = make()
    for host in ("0.0.0.0", "192.168.1.5", "::"):
        with pytest.raises(ValueError):
            StatusGui(runner.board, host=host, port=0)


def test_serves_page_and_state(gui) -> None:
    g, runner, _, _ = gui
    resp, body = get(g, "/")
    assert resp.status == 200 and b"ULTRON" in body and b"/app.js" in body
    assert "frame-ancestors 'none'" in resp.getheader("Content-Security-Policy")
    for asset in ("/app.js", "/app.css"):
        assert get(g, asset)[0].status == 200
    resp, body = get(g, "/api/state")
    state = json.loads(body)
    assert resp.status == 200 and state["runner"]["activity"] == "idle"
    assert set(state) >= {"runner", "current", "queue", "recent", "attention"}


def test_display_only(gui) -> None:
    g, *_ = gui
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        assert get(g, "/api/state", method=method)[0].status == 405
    assert get(g, "/api/jobs/t1/cancel")[0].status == 404


def test_rejects_foreign_host_header(gui) -> None:
    g, *_ = gui
    assert get(g, "/api/state", host="evil.example")[0].status == 403


def test_shows_current_job_live_then_recently_done(gui, library, tmp_path) -> None:
    g, runner, bb, ollama = gui
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    seen: list[dict] = []
    ollama.hook = lambda n: seen.append(json.loads(get(g, "/api/state")[1]))
    runner.tick()
    cur = seen[2]["current"]
    assert seen[2]["runner"]["activity"] == "working"
    assert cur["task_id"] == "t1" and cur["title"] == "title t1" and cur["kind"] == "vision_tag"
    assert cur["model"] == "llava:13b" and cur["total"] == 5 and cur["done"] == 2
    assert cur["percent"] == 40.0 and cur["current_item"].endswith(".png")
    after = json.loads(get(g, "/api/state")[1])
    assert after["current"] is None
    assert after["recent"][0]["task_id"] == "t1" and after["recent"][0]["outputs"] == [str(out)]


def test_closing_gui_mid_job_changes_nothing(make, library, tmp_path) -> None:
    runner, bb, ollama = make(background=True)
    g = StatusGui(runner.board, host="127.0.0.1", port=0).start()
    out = tmp_path / "tags.jsonl"
    bb.add("t1", inputs=[str(library)], output=str(out))
    gate = threading.Event()

    def slow(n: int) -> None:
        if n == 2:
            gate.set()
        time.sleep(0.05)

    ollama.hook = slow
    runner.tick()
    assert gate.wait(5)
    get(g, "/api/state")
    g.close()  # the browser tab / GUI goes away mid-job
    runner._job_thread.join(timeout=10)
    assert bb.tasks["t1"]["status"] == "done" and len(out.read_text().splitlines()) == 5
