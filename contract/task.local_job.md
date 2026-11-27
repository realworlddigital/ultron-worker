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

## `folder_inventory` (model-free NAS walk + unindexed diff)

`model` is omitted (every other kind requires it). `inputs` are folders, absolute or
`ROOT/relative/...` through the media roots, under `runner.allowed_roots` and under a named media
root. Optional `params`: `exclude` (extra folder names to skip; `_index`, `@eaDir`, `#recycle`,
`#snapshot`, `$RECYCLE.BIN`, `System Volume Information` and macOS system folders always are),
`group_depth` (roll the per-folder counts up to the first N folder levels under the root).

```json
{"title": "Inventory GUILD-RAW-Media", "owner": "david", "executor": "local-ultron",
 "job": {"kind": "folder_inventory", "inputs": ["GUILD-RAW-Media"],
         "output": "GUILD-RAW-Media/_inventory/2026-11-22.jsonl", "budget_minutes": 60}}
```

- **Output** (`.jsonl`): one record per media file (vision_tag's image + video extensions),
  `{"root": "GUILD-RAW-Media", "rel": "Guild Hall/2026-10-GH NATICK/C001.MP4",
  "size_bytes": 123, "mtime": "<ISO UTC>", "media": "video"}`, keyed by each folder's innermost
  media root (as vision_tag keys it). Folder symlinks and junctions aren't followed; the walk
  keeps to `budget_minutes`. The output may not be the TOC, `all-tags.jsonl` or a listed index.
- **Diff**: for each root walked, the TOC `<root>/_index/catalog.jsonl` is read (one JSON object
  per indexed folder: `{"root", "folder", "index", "files", "tagged", "skipped", "failed",
  "model", "updated_at"}`, `index` root-relative), then every folder index it lists. A file with
  any record there (tagged, failed or skipped; identity = root + relpath) is indexed. No TOC: the
  note says so and every file is unindexed. An unreadable TOC parks the card; an unreadable
  listed index is named in the note and its files count as unindexed.
- **Result note**: headline (`N media files (... video, ... image, ... GB) in F folders; U
  unindexed in G folders`), then `Unindexed files: U of N` and one line per folder with
  unindexed files, `- <root>/<folder>: <unindexed>/<files>` (cut to fit the note, with a pointer
  to the sidecar). `<output stem>.unindexed.json` beside the output has the full table
  (`folders: [{"root", "folder", "files", "unindexed"}]`).

## `catalog_publish` (NAS catalog -> backbone `record.media_asset`)

Model-free; `model` is omitted. Publishes metadata only: it never moves media bytes and never
deletes a local or backbone record. `inputs` are folders (absolute or `ROOT/relative/...`,
under `runner.allowed_roots` and a named media root). The entries to publish are the tagged
records (a `tags` list, no `failed`/`skipped`) of the folder indexes the root's TOC
`<root>/_index/catalog.jsonl` lists at or under each input folder (`<folder>/tags.jsonl` when the
TOC lists none; no index at all parks the card). `output` is a `.json` report file.

Optional `params`: `org` (the org for every entry) and `orgs` (`{folder ref: org}`, the deepest
matching folder wins over `org`). Values: `guild_hall`, `gcs`, `gea`, `personal`. The org is
never derived from a root or folder name. With neither, the org is `unassigned`; the backbone's
`record.media_asset` schema has no `unassigned` org, so those entries are **held**: hashed, not
published, counted and named in the note.

```json
{"title": "Publish NATICK to the media catalog", "owner": "apollo", "executor": "local-ultron",
 "job": {"kind": "catalog_publish", "inputs": ["GUILD-RAW-Media/Guild Hall/2026-10-GH NATICK"],
         "output": "GUILD-RAW-Media/_publish/2026-11-25-natick.json", "budget_minutes": 120,
         "params": {"orgs": {"GUILD-RAW-Media/Guild Hall": "guild_hall"}}}}
```

- **Selection**: only records of the input's own media root under the input folder; when indexes
  disagree about a file, a tagged record wins over a failed/skipped one (between two tagged, the
  newer file). The report target may
  not be one of the indexes read.
- **Mapping** (one work item, one checkpoint per entry):
  `content_hash` = the record's `content_hash` (`sha256:<hex>`; a bare 64-hex `sha256`/`hash` is
  accepted), else SHA-256 of the file computed now and written back into the folder index, so
  each file is hashed once ever; `kind` = the record's `media` (else by extension);
  `technical` = the record's `technical` (or top-level `duration_sec`/`width`/`height`/`codec`/
  `fps`), else `ffprobe` on the local file (written back too), plus `size_bytes`;
  `tags.structured` = `caption`, `keywords` (the record's `tags`), `folder`, and any
  `structured`/`facets` dict on the record; `tags.flat` = the lowercased keywords and facet
  values; `provenance` = `{feeder: nas_mirror, ingested_by: ultron, ingested_at, source_ref:
  <card id>}` (the backbone stamps `ingested_by` with the writing actor); `rights` = the record's
  `clearance` (or `rights`; a string or `{status}`) when it is `cleared`/`needs_check`/`blocked`,
  else `needs_check`; `org` as above; `locations` = `[{storage_id: nas-main, path:
  "<ROOT>/<relpath>", status: verified, last_verified_at: <publish time>}]` (root-relative: the
  media root name, then the path under it).
- **Upsert** per entry: `GET /records/mas_<sha256(content_hash)[:24]>` (the backbone's
  server-derived id); absent -> `PUT /media/assets` (**created**). Present -> merge: tags and
  technical only gain, the location is added (or re-verified when its stamp is over 7 days old),
  rights move off `needs_check` only; `kind`, `org` and `provenance` stay as first published (an
  entry mapped to a different org is named in the note). Unchanged -> no write (**skipped**);
  changed -> `PUT` (**updated**), and a written version other than read + 1 is named as a
  concurrent write. An entry whose hash was already published by this job is a **duplicate
  collapsed** (its path is one more location on the same asset).
- **Failures**: the backbone unreachable parks the job (retryable; every entry before is
  checkpointed, queued hashes are flushed to the index first) and the next attempt resumes at
  that entry. A 401/403 parks the card for David. Any other refusal (e.g. 422), a missing file,
  a file changed since it was tagged, or an unsupported file type fails just that entry, counted
  and named (only backbone refusals count toward the runner's first-items-all-failed stop).
- **Result note**: `Published N catalog entries from F folder(s) as record.media_asset: C
  created, U updated, S skipped, D duplicates collapsed, H held, X failed, K hashed; A distinct
  asset(s) in the backbone`, one such line per folder, the plan per folder (index count,
  untagged entries not published), then the failed / held entries by name (first 20). The
  report file has every entry (`path`, `folder`, `outcome`, `content_hash`, `asset_id`) and the
  per-folder counts. A second identical job creates nothing: every entry is skipped or a
  duplicate, and nothing is hashed again.

## `clip_segment` (long videos -> virtual clips, backbone `record.media_asset` clips)

Cuts long footage into short, precisely-tagged clips for social use. A clip is a **virtual**
range on its source asset (apollo-backbone clip contract, tsk_db23b53be4aa4333808c): nothing is
cut, copied or stored in the catalog (the NAS listener materializes a clip when a stage request
asks for it). `inputs` are folders (as `catalog_publish`: the TOC's folder indexes at or under
each) and/or asset ids (`mas_<24 hex>`, resolved through the asset's verified `nas-main`
location). `model` is the vision model (default `muse-glimmer:30b`). `output` is a `.json`
report file.

`params`: `org` / `orgs` exactly as `catalog_publish` (deepest folder wins, never derived from a
name). Unmapped footage is **held**: not segmented, not published, named in the note. A mapping
that disagrees with the parent's published org is held too (a clip always carries its parent's
org). Window: `min_sec` 12 / `max_sec` 40 (hard bounds), `target_min_sec` 15 /
`target_max_sec` 30 (where cuts are looked for first); `frames` (per segment, default 3, 1-8),
`max_tags` (15), `scene_threshold` (0.3), `silence_db` (-35), `silence_min_sec` (0.6),
`prompt`, `options`.

- **Per asset** (one work item per parent video; its catalog record must exist, so run
  `catalog_publish` first): videos no longer than `max_sec` are counted `short`, not cut. The
  file is copied to the item's scratch once; one ffmpeg pass finds scene changes (5 fps,
  downscaled, `select=gt(scene,thr)`) and silences (`silencedetect`). Segments are cut greedily:
  the next cut is a speech break (the clip ends just after a silence starts, the next opens just
  before it ends) or a scene change, nearest the middle of the target window, then anywhere in
  the hard bounds, else a hard cut at `target_max_sec`. Dead air at a segment's start and at the
  end of the video is skipped. Ranges are rounded to 0.01 s and deterministic for the same file.
- **Verdict + tags**: the model sees `frames` frames from inside each segment and replies
  `{"usable", "reason", "caption", "tags"}`; usable = a person mid-action or mid-sentence, a
  working build, a demonstration, a reaction; not dead air, setup, an empty room. A kept
  segment needs a caption and tags (vision_tag's strict parsing and filler filter; one repair
  re-ask, then the asset is retried for just the unjudged segments).
- **Clip record** per kept segment, `PUT /media/assets` with `parent_asset_id`,
  `range_start_sec`, `range_end_sec`, `kind` and `org` (the parent's), `rights: null`,
  `tags.structured` = `caption`, `keywords`, `usable_because`, `segmented_by` (the model),
  `tags.flat` = the keywords (the range's own content, never the parent's caption),
  `provenance` = `{feeder: ultron_segmentation, ingested_by: ultron, ingested_at, source_ref:
  <card id>}`; no `content_hash`, `locations` or `technical` (the backbone derives the range's
  technical from the parent). The backbone derives the clip id from parent + range
  (`mas_<sha256("clip:<parent>:<start:.3f>:<end:.3f>")[:24]>`), so a re-run upserts onto the
  same records and never duplicates. A parent without `technical.duration_sec` in the catalog
  fails its asset before any model call (the backbone bounds ranges by it).
- **Resume**: per-asset progress (analysis, each segment's verdict, each published clip id) is
  saved in `<state_dir>/jobs/<card>/segments/<parent id>.json` after every step, so a job killed
  mid-asset resumes at the next unjudged segment (no re-analysis while the file is unchanged);
  finished assets are runner checkpoints and are never segmented again by that card. A backbone
  outage parks the job (retryable) at the clip it was writing; a 401/403 parks it for David;
  any other refusal fails just that clip (named in the note).
- **Result note**: `Segmented A asset(s) into virtual clips: P segments proposed, K kept, H held
  (no org), S too short, F failed; clips C created, U updated, N unchanged`, one line per
  segmented asset (parent id, duration, proposed/kept, created/updated/unchanged), the plan, the
  held and failed assets by name, and the clip ids created. The report file has every asset
  (outcome, parent, ranges kept with ids and captions, rejected ranges with reasons).

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
| media assets (`catalog_publish`) | `GET /records/mas_<24 hex>`, `PUT /media/assets` (body names its `content_hash`) |
| media clips (`clip_segment`) | `GET /records/mas_<24 hex>` (the parent), `PUT /media/assets` (body names `parent_asset_id` + range, no `content_hash`) |

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
