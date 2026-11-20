from __future__ import annotations

import http.client
import json
import sys
import textwrap
import time
from pathlib import Path

import pytest

from ultron_worker.desktop.__main__ import active_python
from ultron_worker.desktop.autostart import Autostart, command_line
from ultron_worker.desktop.controller import tail
from ultron_worker.desktop.instance import SingleInstance
from ultron_worker.desktop.layout import Install, RunnerSettings
from ultron_worker.desktop.server import ShellServer, frame_same_origin
from ultron_worker.desktop.supervisor import RunnerProcess
from ultron_worker.gui import StatusGui


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def status(self) -> dict:
        return {"runner": {"state": "running"}}

    def act(self, action: str, body: dict) -> dict:
        if action == "boom":
            raise ValueError("unknown action")
        self.calls.append((action, body))
        return {"ok": True}


def req(url_port: int, method: str, path: str, *, host=None, token=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", url_port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{url_port}"}
    if token:
        headers["X-Ultron-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    out = resp.read()
    conn.close()
    return resp, out


@pytest.fixture
def shell(make):
    runner, _, _ = make()
    gui = StatusGui(runner.board, host="127.0.0.1", port=0).start()
    ctl = FakeController()
    srv = ShellServer(ctl, gui.server.server_address[:2]).start()
    yield srv, srv.server.server_address[1], ctl, gui
    srv.close()
    gui.close()


def test_frame_rewrite_allows_only_same_origin() -> None:
    out = dict(
        frame_same_origin(
            [
                ("X-Frame-Options", "DENY"),
                ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'"),
                ("Content-Length", "5"),
                ("Cache-Control", "no-store"),
            ]
        )
    )
    assert out["X-Frame-Options"] == "SAMEORIGIN"
    assert out["Content-Security-Policy"] == "default-src 'self'; frame-ancestors 'self'"
    assert "Content-Length" not in out and out["Cache-Control"] == "no-store"


def test_proxies_the_unchanged_status_gui(shell) -> None:
    _, port, _, gui = shell
    resp, body = req(port, "GET", "/")
    direct = req(gui.server.server_address[1], "GET", "/")
    assert resp.status == 200 and body == direct[1] and b"ULTRON" in body
    assert "frame-ancestors 'self'" in resp.getheader("Content-Security-Policy")
    assert resp.getheader("X-Frame-Options") == "SAMEORIGIN"
    # the runner's own port is still unframeable
    assert direct[0].getheader("X-Frame-Options") == "DENY"
    resp, body = req(port, "GET", "/api/state")
    assert resp.status == 200 and "runner" in json.loads(body)


def test_shell_page_carries_token_and_api_needs_it(shell) -> None:
    srv, port, _, _ = shell
    resp, body = req(port, "GET", "/__ultron/")
    assert resp.status == 200 and srv.token.encode() in body
    assert "frame-ancestors 'none'" in resp.getheader("Content-Security-Policy")
    assert req(port, "GET", "/__ultron/api/status")[0].status == 403
    resp, body = req(port, "GET", "/__ultron/api/status", token=srv.token)
    assert resp.status == 200 and json.loads(body)["runner"]["state"] == "running"


def test_controls_need_token_and_loopback_host(shell) -> None:
    srv, port, ctl, _ = shell
    assert req(port, "POST", "/__ultron/api/restart", body={})[0].status == 403
    assert req(port, "POST", "/__ultron/api/restart", token="nope", body={})[0].status == 403
    bad_host = req(port, "POST", "/__ultron/api/restart", token=srv.token, host="evil.example")
    assert bad_host[0].status == 403
    assert req(port, "GET", "/__ultron/", host="evil.example")[0].status == 403
    assert ctl.calls == []
    resp, _ = req(port, "POST", "/__ultron/api/restart", token=srv.token, body={"x": 1})
    assert resp.status == 200 and ctl.calls == [("restart", {"x": 1})]
    assert req(port, "POST", "/__ultron/api/boom", token=srv.token, body={})[0].status == 400
    # nothing is ever POSTed through to the runner GUI
    assert req(port, "POST", "/api/state", token=srv.token, body={})[0].status == 405


def test_proxy_when_runner_gui_is_down(make) -> None:
    srv = ShellServer(FakeController(), ("127.0.0.1", 9)).start()
    try:
        resp, body = req(srv.server.server_address[1], "GET", "/")
        assert resp.status == 502 and b"isn't up" in body
    finally:
        srv.close()


CHILD = textwrap.dedent(
    """
    import sys, time, pathlib
    out = pathlib.Path(sys.argv[1])
    assert sys.argv[-1] == "--stop-on-stdin-close"
    out.write_text("started")
    if "crash" in sys.argv:
        sys.exit(3)
    sys.stdin.buffer.read()  # until the owner closes our stdin
    out.write_text("stopped cleanly")
    """
)


def child(tmp_path: Path, *extra: str) -> tuple[RunnerProcess, Path]:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    marker = tmp_path / "marker"
    proc = RunnerProcess(
        lambda: [sys.executable, str(script), str(marker), *extra], stop_timeout=10
    )
    return proc, marker


def wait_for(cond, timeout: float = 10) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.05)


def test_supervisor_starts_and_stops_cleanly(tmp_path) -> None:
    proc, marker = child(tmp_path)
    assert proc.start() is True and proc.start() is False  # second start is a no-op
    wait_for(lambda: marker.exists())
    assert proc.status()["state"] == "running" and proc.pid
    assert proc.stop() == "clean"
    assert marker.read_text() == "stopped cleanly"
    assert proc.status()["state"] == "stopped" and proc.status()["exit_code"] == 0
    assert proc.stop() == "not running"


def test_supervisor_revives_a_crashed_runner(tmp_path) -> None:
    proc, marker = child(tmp_path, "crash")
    proc.start()
    wait_for(lambda: not proc.running())
    proc.poll(now=0)
    st = proc.status()
    assert st["state"] == "crashed" and st["exit_code"] == 3
    marker.unlink()
    proc.poll(now=1)  # backoff not elapsed
    assert not marker.exists()
    proc.poll(now=10)
    wait_for(lambda: marker.exists())
    proc.stop()
    assert proc.status()["state"] == "stopped"


def test_stopped_runner_is_not_revived(tmp_path) -> None:
    proc, marker = child(tmp_path)
    proc.start()
    wait_for(lambda: marker.exists())
    proc.stop()
    proc.poll(now=1e9)
    assert not proc.running()


def test_real_runner_stops_when_stdin_closes(tmp_path) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            [backbone]
            url = "http://127.0.0.1:9"
            [runner]
            state_dir = {json.dumps(str(tmp_path / "state"))}
            allowed_roots = [{json.dumps(str(tmp_path))}]
            [gui]
            enabled = false
            """
        )
    )
    proc = RunnerProcess(
        lambda: [sys.executable, "-m", "ultron_worker", "--config", str(cfg)], stop_timeout=20
    )
    import os

    os.environ["ULTRON_BACKBONE_KEY"] = "test-key"
    try:
        proc.start()
        log = tmp_path / "state" / "ultron.log"
        wait_for(lambda: log.exists() and "ultron-runner" in log.read_text(), timeout=20)
        assert proc.stop() == "clean"
        assert proc.status()["exit_code"] == 0
        assert "stopping" in log.read_text()
    finally:
        os.environ.pop("ULTRON_BACKBONE_KEY", None)
        proc.stop()


class FakeRegistry:
    def __init__(self) -> None:
        self.value: str | None = None

    def get(self) -> str | None:
        return self.value

    def set(self, value: str) -> None:
        self.value = value

    def delete(self) -> None:
        self.value = None


def test_autostart_toggle_and_refresh() -> None:
    reg = FakeRegistry()
    a = Autostart([r"C:\ProgramData\Ultron\Ultron.exe", "--background"], reg)
    assert a.supported and not a.enabled()
    assert a.set(True) is True
    assert reg.value == r"C:\ProgramData\Ultron\Ultron.exe --background"
    a.argv = [r"C:\Program Files\Ultron\Ultron.exe", "--background"]
    a.refresh()
    assert reg.value == '"C:\\Program Files\\Ultron\\Ultron.exe" --background'
    assert a.set(False) is False and reg.value is None
    a.refresh()  # disabled stays disabled
    assert reg.value is None
    assert command_line(["a b", "c"]) == '"a b" c'


def test_autostart_unsupported_off_windows() -> None:
    a = Autostart(["x"], None)
    assert not a.supported and not a.enabled()
    with pytest.raises(RuntimeError):
        a.set(True)


def test_single_instance_second_launch_shows_first() -> None:
    first, second = SingleInstance(port=0), None
    assert first.acquire()
    port = first.sock.getsockname()[1]
    shown: list[bool] = []
    first.serve(lambda: shown.append(True))
    second = SingleInstance(port=port)
    try:
        assert second.acquire() is False
        assert second.notify_existing() is True
        wait_for(lambda: shown == [True])
    finally:
        first.release()
    assert second.acquire(wait=2) is True
    second.release()


def test_install_record_and_settings(tmp_path) -> None:
    root = tmp_path / "Ultron"
    inst = Install(root)
    assert inst.sha is None and inst.repo is None and inst.extras == ["desktop"]
    inst.save(repo=str(tmp_path), venv=str(root / "venv"), sha="a" * 40, extras=["desktop"])
    # a BOM (Windows PowerShell 5 Set-Content -Encoding UTF8) still reads
    (root / "install.json").write_bytes(b"\xef\xbb\xbf" + (root / "install.json").read_bytes())
    again = Install(root)
    assert again.sha == "a" * 40 and again.venv == root / "venv"
    cmd = again.runner_command()
    assert cmd[1:4] == ["-m", "ultron_worker", "--config"] and cmd[-1] == str(root / "config.toml")
    (root / "config.toml").write_text(
        '[runner]\nstate_dir = "/tmp/ultron-state"\n[gui]\nport = 9999\n'
    )
    s = RunnerSettings.read(root / "config.toml")
    assert s.state_dir == Path("/tmp/ultron-state") and s.gui_port == 9999
    assert s.log_path.name == "ultron.log"


def test_prune_keeps_current_and_previous(tmp_path) -> None:
    root = tmp_path
    for name in ("old1", "prev", "cur"):
        (root / "venvs" / name).mkdir(parents=True)
    inst = Install(root)
    inst.save(venv=str(root / "venvs" / "cur"), previous_venv=str(root / "venvs" / "prev"))
    removed = inst.prune_venvs()
    assert [p.name for p in removed] == ["old1"]
    assert sorted(p.name for p in (root / "venvs").iterdir()) == ["cur", "prev"]


def test_tail_reads_the_end(tmp_path) -> None:
    log = tmp_path / "ultron.log"
    log.write_text("".join(f"line {i}\n" for i in range(5000)))
    lines = tail(log, max_bytes=2000, max_lines=50)
    assert lines[-1] == "line 4999" and len(lines) == 50
    assert tail(tmp_path / "missing.log") == []


def test_app_moves_to_the_active_venv(tmp_path: Path) -> None:
    inst = Install(tmp_path)
    assert active_python(inst) is None  # no install.json: run as is
    inst.save(venv=sys.prefix)
    assert active_python(inst) is None  # already on the active venv
    new = tmp_path / "venvs" / "abc"
    exe = new / ("Scripts/pythonw.exe" if sys.platform == "win32" else "bin/python")
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    inst.save(venv=str(new))
    assert active_python(inst) == exe  # an old shortcut/venv: switch to the updated install
