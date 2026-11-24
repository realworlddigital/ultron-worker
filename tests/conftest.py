from __future__ import annotations

import base64
import itertools
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from ultron_worker.backbone import Backbone, BackboneError, BackboneUnavailable
from ultron_worker.config import Config
from ultron_worker.ollama import OllamaUnavailable
from ultron_worker.runner import Runner

# The task.local_job lifecycle the runner is built against (contract/task.local_job.md).
LOCAL_JOB_TRANSITIONS = {
    "backlog": ["dispatched", "cancelled"],
    "dispatched": ["in_progress", "backlog", "cancelled"],
    "in_progress": ["done", "needs_david", "cancelled"],
    "needs_david": ["dispatched", "done", "cancelled"],
}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeBackbone(Backbone):
    """In-memory backbone behind the real client (so the client's scope checks run)."""

    def __init__(self, cfg: Config) -> None:
        super().__init__(cfg)
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.notes: list[tuple[str, dict[str, Any]]] = []
        self.transitions: list[tuple[str, str]] = []
        self.statuses: list[dict[str, Any]] = []
        self.sent: list[tuple[str, str]] = []
        self.timeouts: list[float] = []
        self.down = False
        self.me_actor = cfg.actor
        self._seq = itertools.count(1)
        self.media: dict[str, dict[str, Any]] = {}  # "<kind>:<id>" -> last body
        self.media_calls: list[tuple[str, str]] = []
        self.paused: set[str] = set()
        self.syncs: dict[str, dict[str, Any]] = {}

    def add(
        self,
        tid: str,
        *,
        kind: str = "vision_tag",
        status: str = "dispatched",
        type_: str = "task.local_job",
        domain: str | None = None,
        executor: str = "local-ultron",
        **job: Any,
    ) -> dict[str, Any]:
        task = {
            "id": tid,
            "type": type_,
            "domain": domain or self.domain,
            "status": status,
            "created_at": f"2026-11-18T00:00:{next(self._seq):02d}+00:00",
            "data": {
                "title": f"title {tid}",
                "executor": executor,
                "job": {"kind": kind, "model": "llava:13b", "budget_minutes": 600, **job},
            },
        }
        self.tasks[tid] = task
        return task

    def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        *,
        timeout: float = 30.0,
    ) -> Any:
        self.sent.append((method, path))
        self.timeouts.append(timeout)
        if self.down:
            raise BackboneUnavailable("connection refused")
        if path == "/me":
            return {"actor": {"id": self.me_actor, "kind": "agent"}}
        if path == "/tasks":
            p = params or {}
            return {
                "items": [
                    t
                    for t in self.tasks.values()
                    if t["domain"] == p.get("domain") and t["status"] == p.get("status")
                ]
            }
        parts = path.strip("/").split("/")
        if parts[0] == "tasks":
            task = self.tasks.get(parts[1])
            if task is None:
                raise BackboneError(404, "no such task")
            if len(parts) == 2:
                return task
            if parts[2] == "transition":
                to = (body or {})["to"]
                if to not in LOCAL_JOB_TRANSITIONS.get(task["status"], []):
                    raise BackboneError(422, f"{task['status']} -> {to} not allowed")
                task["status"] = to
                self.transitions.append((task["id"], to))
                return task
            if parts[2] == "notes":
                self.notes.append((task["id"], dict(body or {})))
                return {"id": f"n{len(self.notes)}"}
        if path == "/events":
            self.events.append(dict(body or {}))
            return {"id": f"e{len(self.events)}"}
        if path.startswith("/system/status/"):
            self.statuses.append(dict(body or {}))
            return {"ok": True}
        if parts[0] == "media":
            return self._media(method, parts[1:], params, body or {})
        raise AssertionError(f"unexpected {method} {path}")

    def _media(self, method: str, parts: list[str], params: Any, body: dict[str, Any]) -> Any:
        kind = parts[0]
        if kind == "inbox" and method == "GET":
            feeders = [
                {"data": {"feeder_id": k.split(":", 1)[1], **v,
                          "status": "paused" if k.split(":", 1)[1] in self.paused else "connected"}}
                for k, v in self.media.items() if k.startswith("feeders:")
            ]  # fmt: skip
            return {"feeders": feeders, "status": None, "items": []}
        if kind == "sync" and method == "GET":
            status = (params or {})["status"]
            return {"items": [c for c in self.syncs.values() if c["status"] == status]}
        if kind == "sync":
            cmd = self.syncs[parts[1]]
            self.media_calls.append((parts[2], parts[1]))
            if parts[2] == "ack":
                cmd["status"] = "acked"
                cmd["data"]["acked_by"] = self.actor
            else:
                cmd["status"] = "done" if body["ok"] else "failed"
                cmd["data"].update(items=body["items"], error=body["error"])
            return cmd
        if kind == "feeders" and len(parts) == 3:
            if f"feeders:{parts[1]}" not in self.media:
                raise BackboneError(404, f"no media feeder {parts[1]!r}")
            key = f"pull:{parts[1]}"
        else:
            key = f"{kind}:{parts[1]}"
        if kind == "ingest":
            sums = {
                k: sum(s[k] for s in body["sources"])
                for k in ("photos_waiting", "videos_waiting", "bytes_waiting")
            }
            assert sums == body["totals"]
        self.media_calls.append((kind if len(parts) < 3 else "pull", parts[1]))
        self.media[key] = {**self.media.get(key, {}), **body}
        return {"ok": True}

    def sync_command(self, cid: str, feeder_id: str, action: str = "rescan") -> None:
        self.syncs[cid] = {
            "id": cid,
            "status": "pending",
            "data": {"feeder_id": feeder_id, "action": action, "requested_by": "david"},
        }

    def actions(self, tid: str | None = None) -> list[str]:
        return [
            e["action"]
            for e in self.events
            if tid is None or (e.get("target") or {}).get("id") == tid
        ]


class FakeOllama:
    """Stands in for the local Ollama: records calls, can go down or call a hook per call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.down = False
        self.hook: Any = None
        self.lock = threading.Lock()

    def generate(
        self,
        model: str,
        prompt: str,
        *,
        images: list[Path] | None = None,
        json_format: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
        with self.lock:
            if self.down:
                raise OllamaUnavailable("ollama not reachable at http://127.0.0.1:11434")
            self.calls.append(
                {"model": model, "images": [str(i) for i in images or []], "prompt": prompt}
            )
            n = len(self.calls)
        if self.hook:
            self.hook(n)
        if images:
            name = Path(images[0]).stem
            return json.dumps({"caption": f"a photo {name}", "tags": [name, "Red", "red", "cat"]})
        return f"answer {n}"

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        with self.lock:
            if self.down:
                raise OllamaUnavailable("down")
            self.calls.append({"model": model, "texts": texts})
        return [[0.1, 0.2, float(len(t))] for t in texts]


@pytest.fixture(autouse=True)
def _no_budget_grace(monkeypatch) -> None:
    monkeypatch.setattr("ultron_worker.runner.BUDGET_GRACE", 0)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "library"
    (lib / "sub").mkdir(parents=True)
    for name in ("a.png", "b.png", "c.png", "sub/d.png", "sub/e.png"):
        (lib / name).write_bytes(PNG)
    (lib / "notes.txt").write_text("not an image")
    return lib


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        backbone_url="https://backbone.example",
        key="test-key",
        state_dir=tmp_path / "state",
        allowed_roots=(tmp_path,),
        poll_seconds=1,
        heartbeat_seconds=1,
        progress_seconds=0,
        max_attempts=3,
        gui_enabled=False,
        host_name="ultron-test",
    )


@pytest.fixture
def make(cfg: Config):
    """make(bb=None, ollama=None) -> (runner, bb, ollama); a second call with the same bb and
    ollama is a runner restart over the same state dir."""
    made: list[Runner] = []

    def _make(
        bb: FakeBackbone | None = None,
        ollama: FakeOllama | None = None,
        config: Config | None = None,
        background: bool = False,
    ):
        c = config or cfg
        bb = bb or FakeBackbone(c)
        ollama = ollama or FakeOllama()
        runner = Runner(c, backbone=bb, ollama=ollama, background=background)
        made.append(runner)
        return runner, bb, ollama

    yield _make
    for r in made:
        r.stop.set()
        r.store.close()
