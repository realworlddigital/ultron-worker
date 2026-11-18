# Card contract: `task.local_job` → `local-ultron`

The runner (this repo) is built against the contract below. The backbone side (schema, lifecycle,
domain, event actions, the runner actor and its key) is Apollo/David's to add; nothing here
creates an actor, a key or a grant.

## Card

| field | value |
|---|---|
| `type` | `task.local_job` (never `task.dev_build`) |
| `domain` | the runner's domain, `compute` by default (`backbone.domain` in the runner config); never `dev`, `care`, `household` or `finance` |
| `data.executor` | `local-ultron` |
| `data.job` | `kind`, `inputs`, `model`, `output`, `budget_minutes`, optional `params` — schema: [`task.local_job.json`](task.local_job.json) |

Example `data`:

```json
{"title": "Tag the 2019 Cape photos", "owner": "david", "executor": "local-ultron",
 "job": {"kind": "vision_tag", "inputs": ["D:\\Photos\\2019 Cape"], "model": "llava:13b",
         "output": "D:\\Photos\\2019 Cape\\tags.jsonl", "budget_minutes": 600,
         "params": {"max_tags": 20, "video_frames": 4}}}
```

## Lifecycle (`task.states.local_job`)

```
backlog -> dispatched | cancelled
dispatched -> in_progress | backlog | cancelled      (runner claims: dispatched -> in_progress)
in_progress -> done | needs_david | cancelled        (runner closes: -> done, or -> needs_david)
needs_david -> dispatched | done | cancelled         (David re-dispatches: resumes from checkpoint)
```

No `needs_review`, `merge_ready` or `going_live`: a local job has no PR and no go-live.
`transition_actors` should reserve `dispatched -> in_progress`, `in_progress -> done` and
`in_progress -> needs_david` to the runner actor (and David).

## What the runner writes

| what | call |
|---|---|
| claim / close | `POST /tasks/{id}/transition` (`in_progress`, `done`, `needs_david`) |
| result / failure note | `POST /tasks/{id}/notes` (`update` on success, `action` on needs_david) |
| events | `POST /events`, actions `ultron.claimed`, `ultron.resumed`, `ultron.progress`, `ultron.retry`, `ultron.completed`, `ultron.failed`, target = the card, domain = the runner's domain |
| heartbeat | `PUT /system/status/ultron-runner` (`state`: `idle` / `working` / `paused-by-sleep`) |

It reads only `GET /me` and `GET /tasks?domain=<its domain>&status=dispatched|in_progress`
(and keeps only `task.local_job` + `local-ultron` cards from that). The client refuses any other
path, domain, actor or event prefix before a request is sent (`backbone.py`, `tests/test_scope.py`).

## Backbone changes this needs (Apollo card, not this repo)

1. `TASK_TYPES += ("task.local_job",)` with the schema above, and the `task.states.local_job` policy.
2. The runner's domain (`compute`, or whichever David picks) in the domain registry.
3. `ultron.*` in the event-action allowlist (`ALLOWED_EVENT_ACTIONS`).
4. A dedicated agent actor `ultron-runner` with its own key, scoped to read/write tasks and append
   events in that one domain only, plus its own `system/status_ultron-runner`. Issued by David.

## Why Gilfoyle never picks these up

Three independent layers, checked against Gilfoyle at `realworlddigital/gilfoyle@ff2edb2`:

1. **Domain.** Every Gilfoyle pass (dispatch, Codex, supervise, merge watch, go-live lane) lists
   `GET /tasks?domain=dev`. Local cards are in the runner's domain, so Gilfoyle never reads them.
2. **Type/executor.** Intake (`dispatch.py`, `codex.py`) keeps only `type == "task.dev_build"`;
   supervise and merge watch only act on `devin`/`codex-*` executors.
3. **Lifecycle.** The go-live lane keys on status `going_live` alone; `task.local_job` has no such
   state, so even a misfiled card can't enter it.

[`test_gilfoyle_ignores_local_jobs.py`](test_gilfoyle_ignores_local_jobs.py) runs inside Gilfoyle's
own test suite and proves 1 (with the backbone's domain filter: zero requests touch any local
card, in any status) and 2+3 (with the filter switched off: still zero, in every `task.local_job`
state). Live proof on Ultron: after the acceptance run, `GET /events?actor=gilfoyle` has no event
targeting the test card, and `GET /events?domain=compute` shows only `ultron.*` / David actions.
