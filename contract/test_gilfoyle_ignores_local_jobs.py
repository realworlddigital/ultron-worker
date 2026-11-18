"""Proof that Gilfoyle never touches a ``task.local_job`` / ``local-ultron`` card.

Runs inside Gilfoyle's own test suite (its FakeBackbone, Devin and GitHub fakes):

    cp contract/test_gilfoyle_ignores_local_jobs.py ../gilfoyle/tests/
    cd ../gilfoyle && python -m pytest -q tests/test_gilfoyle_ignores_local_jobs.py

Two layers, each tested on its own:

1. Domain. Every Gilfoyle pass lists ``domain=dev`` only; local cards live in the runner's own
   domain, so with the backbone's domain filter Gilfoyle never even reads them, in any status.
2. Type + executor + lifecycle. Gilfoyle's FakeBackbone ignores ``domain``, so with the filter
   off every pass is handed the local cards anyway. In every state the ``task.local_job``
   lifecycle has, the intake (``task.dev_build`` only), supervisor and merge watcher
   (devin/codex executors only) still leave them alone. The go-live lane keys on status alone,
   which is why ``task.local_job`` has no ``going_live``/``merge_ready`` state.
"""

from __future__ import annotations

import httpx

# Every status Gilfoyle scans, including ones a local card can never be in.
ALL_STATUSES = (
    "backlog",
    "dispatched",
    "in_progress",
    "needs_review",
    "merge_ready",
    "going_live",
    "needs_david",
    "done",
    "cancelled",
)
# The task.local_job lifecycle (contract/task.local_job.md).
LOCAL_JOB_STATUSES = ("backlog", "dispatched", "in_progress", "needs_david", "done", "cancelled")


def _local_card(tid: str, status: str) -> dict:
    return {
        "id": tid,
        "type": "task.local_job",
        "domain": "compute",
        "status": status,
        "version": 2,
        "created_at": f"2026-11-18T00:{tid}",
        "data": {
            "title": f"vision-tag {tid}",
            "owner": "david",
            "executor": "local-ultron",
            "repo": "realworlddigital/ultron-worker",
            "job": {
                "kind": "vision_tag",
                "inputs": ["D:/Photos/test"],
                "model": "llava:13b",
                "output": "D:/Photos/test/tags.jsonl",
                "budget_minutes": 600,
            },
        },
    }


def _domain_filtered(bb):
    """The real backbone filters GET /tasks by ``domain``; Gilfoyle's fake doesn't."""
    raw = bb.handler

    def handler(request: httpx.Request) -> httpx.Response:
        resp = raw(request)
        domain = request.url.params.get("domain")
        if (request.method, request.url.path) == ("GET", "/api/v1/tasks") and domain:
            items = [t for t in resp.json()["items"] if t.get("domain", "dev") == domain]
            return httpx.Response(resp.status_code, json={"items": items})
        return resp

    bb.handler = handler


async def _run_gilfoyle(bb, devin, make, statuses) -> dict[str, str]:
    local = {f"lu{i}": s for i, s in enumerate(statuses)}
    bb.tasks = [t for t in bb.tasks if t["id"] != "t1"]
    for tid, status in local.items():
        bb.tasks.append(_local_card(tid, status))
    bb.add_task("t9")  # a real Devin card in the same backbone, so every pass does run
    g = make(with_devin=True, with_github=True)
    await g.boot()
    for _ in range(4):
        await g.tick()
    assert devin.sessions and "t9" in devin.sessions[0]["prompt"]  # Gilfoyle was working
    return local


def _assert_untouched(bb, devin, local: dict[str, str]) -> None:
    touched = []
    for r in bb.requests:
        path = r.url.path.removeprefix("/api/v1")
        body = r.content.decode() if r.content else ""
        if any(f"/{tid}" in path or f'"{tid}"' in body for tid in local):
            touched.append((r.method, path, body[:160]))
    assert touched == [], touched
    assert [t["id"] for t in bb.tasks if t["id"] in local and t["status"] != local[t["id"]]] == []
    assert not [e for e in bb.events if (e.get("target") or {}).get("id") in local]
    assert not [r for r in bb.records.values() if (r.get("data") or {}).get("task_id") in local]
    assert not any(tid in s["prompt"] for s in devin.sessions for tid in local)


async def test_gilfoyle_never_reads_the_runner_domain(bb, devin, make) -> None:
    _domain_filtered(bb)
    local = await _run_gilfoyle(bb, devin, make, ALL_STATUSES)
    _assert_untouched(bb, devin, local)
    listed = {
        r.url.params.get("domain")
        for r in bb.requests
        if (r.method, r.url.path) == ("GET", "/api/v1/tasks")
    }
    assert listed == {"dev"}


async def test_gilfoyle_ignores_local_cards_even_unfiltered(bb, devin, make) -> None:
    local = await _run_gilfoyle(bb, devin, make, LOCAL_JOB_STATUSES)
    _assert_untouched(bb, devin, local)
