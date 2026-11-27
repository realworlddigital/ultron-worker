# ultron-worker

The **local route** for batch work: a small Windows service on David's Surface **Ultron** that picks
up backbone cards routed to it (`type: task.local_job`, `data.executor: local-ultron`) and runs them
on local models (Ollama, faster-whisper), so cataloging the photo/video library, batch inference and
transcription burn no Devin ACU or API tokens. A localhost status page shows it working.

- **Outbound only.** The runner polls the backbone over HTTPS. Nothing listens on the LAN: Ollama
  stays on `127.0.0.1:11434` and the status GUI on `127.0.0.1:8765`.
- **Own key, own cards.** It uses a dedicated `ultron-runner` actor and key that David issues.
  It reads only its own domain's `task.local_job` cards and writes only their transitions, notes
  and `ultron.*` events. `care`, `household` and `finance` (and `personal`) are refused outright.
- **Never touched by Gilfoyle.** Gilfoyle only reads the `dev` domain and only takes
  `task.dev_build` cards. See [`contract/task.local_job.md`](contract/task.local_job.md).

## Job kinds (v1)

| `kind` | does | `model` | `output` |
|---|---|---|---|
| `vision_tag` | captions and tags images, plus frames sampled from videos (ffmpeg) | Ollama vision model, e.g. `llava:13b`, `qwen2.5vl:7b` | `.jsonl` (one line per file) or `.json` |
| `text_batch` | runs `params.prompt` (with `{text}`) over each `.txt/.md/.json/.jsonl/.csv` item | Ollama text model, e.g. `qwen2.5:14b` | `.jsonl` / `.json` |
| `embeddings` | embeds each text item | Ollama embedding model, e.g. `nomic-embed-text` | `.jsonl` / `.json` |
| `transcribe` | transcribes audio/video with faster-whisper (if installed) | whisper size, e.g. `large-v3` | folder: `<name>.txt` + `<name>.segments.json` per file, plus `index.json` |
| `catalog_publish` | model-free: publishes the tagged entries of the folders' indexes (via `<root>/_index/catalog.jsonl`) as backbone `record.media_asset` (hashing once and probing where missing; idempotent upsert); org only from `params.org`/`params.orgs` | none (omit `model`) | `.json` report (per-folder created/updated/skipped/duplicates/held/failed) |
| `folder_inventory` | model-free stat walk of media folders (vision_tag extensions) + unindexed diff against `<root>/_index/catalog.jsonl` | none (omit `model`) | `.jsonl` (one `{root, rel, size_bytes, mtime, media}` per file) + `<stem>.unindexed.json` |
| `clip_segment` | cuts long published videos into virtual 12-40 s clips (ffmpeg scene + silence boundaries, vision-model usability verdict and per-range caption/tags), upserted as backbone clip records (`record.media_asset` with `parent_asset_id` + range; org = the parent's, rights from the parent); no bytes cut or copied into the catalog; org mapping as `catalog_publish`, unmapped footage held | Ollama vision model, default `muse-glimmer:30b` | `.json` report (per-asset proposed/kept/held + clip ids) |

ComfyUI/media generation is not in v1. To add a kind, write a module that subclasses
`ultron_worker.jobs.JobKind` and calls `register(...)`, then list it under `runner.job_modules`
(or publish it under the `ultron_worker.jobs` entry point). The polling core doesn't change.

A card's `data`:

```json
{"title": "Tag the 2019 Cape photos", "owner": "david", "executor": "local-ultron",
 "job": {"kind": "vision_tag", "inputs": ["D:\\Photos\\2019 Cape"], "model": "llava:13b",
         "output": "D:\\Photos\\2019 Cape\\tags.jsonl", "budget_minutes": 600}}
```

`budget_minutes` is the job's wall-clock budget. It counts active time only, so sleep doesn't use
it up. Inputs and outputs must sit under `runner.allowed_roots` (Dropbox included if you list it).

**Media roots (NAS-swap-proof paths).** Each `runner.allowed_roots` entry is also a named media
root, named after its final folder (`\\GUILD-NAS\GUILD-RAW-Media` is `GUILD-RAW-Media`); an
optional `[media_roots]` table (`name = "base path"`) overrides or adds names. Inputs, outputs
and `params.merge` take an absolute path or the `ROOT/relative/...` form, e.g.
`"GUILD-RAW-Media/Guild Hall/September"`; either way the resolved path must sit under the allowed
roots. Every `vision_tag` record is keyed by `root` + `relpath` (forward slashes); its absolute
`path` is kept for debugging only. When the NAS moves, point the root at the new base and the
index still resolves. Index records from older runners that carry only an absolute `path` are
normalized on read when the path is under a known root. `params.merge` (a list of earlier index
files) merges them into this job's output, one record per `root` + `relpath`; this pass wins,
except that a failed or skipped record never replaces a tagged one.

**Catalog (cards are disposable).** Every `vision_tag` record carries `size_bytes` and `mtime`
(epoch seconds) from the file's stat at tag time. When a job starts and its output index
already exists, every well-formed tagged record whose file still has the same size and mtime
(2 s slack for FAT/SMB copies) counts as done from the start, with no model call; a changed file
is tagged again, a new file is tagged, and a record written before stamps existed is trusted once
and backfilled with the current stat. So a superseding card, or a re-run of a folder with new
files, tags only what is new or changed (`params.ignore_index: true` re-tags everything). The
SQLite checkpoint stays the within-run crash layer. After each completion the media root's
`_index\catalog.jsonl` (one entry per indexed folder: `root`, `folder`, `index`, `files`,
`tagged`, `skipped`, `failed`, `model`, `updated_at`; upserted on `root` + `folder`, where
`folder` is the job's input folder, or the index's folder when the input isn't one folder) is
updated and `_index\all-tags.jsonl` is rebuilt from every listed index with the merge rules
above. A missing or unreadable index is left out and named in the completion note; catalog
trouble never fails the job (its own index is already written). At startup the runner removes
frame scratch dirs a hard kill left under `state_dir\jobs\<task>\frames`.

## How a job runs

1. **Poll.** Every `poll_seconds` (60 by default) the runner lists its `in_progress` cards first
   (to resume them) and then its `dispatched` cards, oldest first. It claims one with
   `dispatched → in_progress` and posts `ultron.claimed`.
2. **Checkpoint.** Each finished item is committed to SQLite (`state_dir\state.db`, WAL,
   synchronous=FULL) in the same transaction as the job's progress. If the runner is killed,
   rebooted or the laptop sleeps, it picks up after the last committed item (`ultron.resumed`) and
   never runs a finished item through the model again. Each finished item logs one INFO line
   with where its time went, e.g. `item GUILD-RAW-Media/Guild Hall/C8562.MP4 done in 83.2s
   (stat 0.0s, frames 12.0s, model 70.1s x2, other 0.1s, post 0.3s)` (`x2`: a repair re-ask;
   `incl. ~Ns asleep` when the laptop slept mid-item); the phases are also kept in the item's
   checkpoint row (`items.timing`). Enumeration is logged once per job. A video is copied once
   into the item's scratch dir (in a child process with a deadline of 90 s + size at 5 MB/s,
   so a stalled share read fails the item instead of holding the queue) and every
   ffprobe/ffmpeg seek reads that local copy (per-frame seeks over the NAS share hung); a frame call slower than 90 s is killed and logged as a
   WARNING naming the file and frame index, and video lines end with `frame_timeouts N` (also
   in the record and checkpoint as `frame_timeouts`, and in progress events'
   `last_item.counts`).
3. **Progress.** `ultron.progress` events (done/total, current file) go out at most every
   `progress_seconds`. For job kinds that time phases (vision_tag, and every kind that calls the
   model), an event sent after an item appends that item's phases after the unchanged
   `Ultron <task>: N/M (P%)` prefix, e.g. `Ultron t1: 31/67 (46.3%) - last item Guild
   Hall/2026-10-GH NATICK/C8563.MP4: stat 0.0s, frames 12.0s, model 70.1s, other 0.1s, post
   0.3s`, or `... - last item <file> failed (<phases>): <error>` for a failed/skipped item; the
   same values ride in the event data as `last_item` (`name`, `outcome`, `phases`, `total_s`,
   `error`). A heartbeat goes to `system/status/ultron-runner` (`idle` / `working` /
   `paused-by-sleep`). The job thread never waits on the backbone between items: its events go to
   a small in-memory outbox (newest progress per card kept while the backbone is down) that a
   sender thread and the poll loop flush; every backbone write gives up after 10 s. A dead tunnel
   costs zero item time.
4. **Output.** The output file is written once, atomically (temp file then `os.replace`), so a
   crash never leaves a half-written or duplicated file. The runner then posts `ultron.completed`,
   adds a result note and moves the card to `done`.
5. **Failure.** A job is retried at most `max_attempts` times (`ultron.retry`), only for
   retryable errors (e.g. Ollama down). After that, or right away for a bad spec, an unknown kind
   or a path outside the allowed roots, the card goes to **`needs_david`** with the error in an
   `ultron.failed` event and an action note. Re-dispatching it resumes from the checkpoint.

   **vision_tag replies** are parsed strictly: a record needs a non-empty `caption` and a JSON
   array of `tags` (generic people, setting and mood words such as `man`, `one person`,
   `indoor`, `serious` are dropped). A reply that doesn't parse gets one repair re-ask; if that
   fails too, the item is checkpointed as `failed` (the output line carries only `path` and
   `failed`, never caption/tags) and the job ends its pass with a retryable error, so the next
   attempt re-runs just those items. Still failing after `max_attempts`, the card goes to
   `needs_david`; re-dispatching retries them again.
6. **Offline / asleep.** Cards just stay in `dispatched`, visibly, on the board. Ultron's
   heartbeat stops. They start on the first poll after it wakes. If the backbone is unreachable in
   the middle of a job, the job keeps running locally and reports when the backbone is back.

## Media inbox watcher

`[media_inbox]` (off by default) makes the runner the single ingest point for the NAS media
inbox, `<root>/<inbox>/<source>/<YYYY-MM-DD>/`, in its own thread beside the card loop. Each
pass (every `interval_seconds`, and at once for a backbone sync command naming one of its feeders):

- **Inventory + dedupe**: new or changed files are hashed (sha256; unchanged size + mtime reuse
  the stored hash). One record per content; the same bytes arriving again are noted on that
  record (`duplicates`) and left in place, never a second record. Files younger than
  `settle_seconds` wait for the next pass.
- **Tagging**: waiting items are tagged with `vision_tag`'s model call (Glimmer by default),
  `tag_per_pass` at a time, and only while no card job holds the model.
- **Filing**: the one subject folder whose `[media_inbox.subjects]` tags best match gets the
  file, as `<subject>/<YYYY-MM-DD>/<name>`. No match, a tie, or no usable tags after
  `review_after_attempts` = the file stays in the inbox flagged `review`. A move is recorded
  before it starts and finished (or redone) by the next pass after a kill.
- **Catalog**: `_index/inbox-tags.jsonl` (root + relpath, size + mtime, sha256, feeder, review
  reason or `failed`; never an absolute path) is upserted into `_index/catalog.jsonl` and
  merged into `all-tags.jsonl` through the same machinery as `vision_tag`.
- **Backbone** (apollo-backbone `docs/media-inbox.md`): feeders registered with
  `worker = ultron-runner`, one `record.media_item` per content, the heartbeat
  (`PUT /media/ingest/{watcher}`: photos / videos / bytes still in the inbox per source and in
  total, last run status) and, for native feeders, the last pull inferred from the newest file
  in their folder. Sync commands are acked and finished: an api feeder runs its registered
  puller (`ultron_worker.inbox.register_puller`) then re-scans; a native feeder only re-scans:
  no Synology pull is ever claimed.

Every write goes through a durable SQLite outbox (`<state_dir>/inbox.sqlite3`), one row per
record with the newest state winning, replayed after an outage or a restart. The runner still
can't be stopped remotely; a local stop interrupts a pass between files and the next pass resumes.

## Media feeders (Discord thread, Google Drive folder)

`[media_feeders.discord]` and `[media_feeders.gdrive]` (off by default, need `[media_inbox]`) are
the two api feeders. Each pulls every `interval_seconds` and at once on a Sync now command, lands
files only in `<inbox>/<folder>/<YYYY-MM-DD>/` (the pull's local date) with one
`manifest.jsonl` line per file, and reports the pull to the feeder registry (`last_pull_at`,
`items_24h`, `items_total`, status `connected` / `error` + `last_error`). Tagging, filing and the
catalog are the watcher's. A feeder David paused in the registry skips its cadence pulls.

- **Discord**: one thread (`guild_id` + `thread_id`); image attachments only; manifest line =
  attachment id (`source_id`), `message_id`, `author`, `timestamp`. Incremental by the last
  message id per thread; a known message or attachment id is never downloaded again.
- **Google Drive**: one folder (`folder_id`); `image/*` and `video/*` only; manifest line =
  `drive_file_id`, `name`, `modified_time`. Incremental by a modified-time watermark per folder;
  an edited file (new modified time) lands again, the watcher dedupes the bytes.
- **Credentials are David's**: the bot token comes from `ULTRON_DISCORD_TOKEN` or `token_file`;
  the Drive sign-in is the authorized-user JSON at `auth_file`, written once by
  `python -m ultron_worker.feeds.gdrive_auth --client-secrets <client.json> --out <auth_file>`
  (Google's standard browser flow, scope `drive.readonly`; `pip install ".[gdrive-auth]"`). The
  feeders only read them. Missing id or credential = status error `not configured: ...`; a
  token Discord refuses or a sign-in Google says is expired = status error, and the feeder stays
  stopped until the token file / sign-in changes. Nothing is prompted for, generated or stored.
- **Read-only**: the only network calls are GETs to `discord.com`, the Discord CDN and
  `www.googleapis.com`, plus the OAuth token refresh; attachments are fetched without the bot
  token, and a credential never follows a redirect to another host.

Pull state (last message id, watermark, every file landed) is `<state_dir>/feeds.sqlite3`.
`ultron-runner --check` prints each feeder's readiness.

## Status GUI

The desktop app shows it in its window; it is also at **http://127.0.0.1:8765/**. It shows the runner state, the current job (card title
and id, kind, model, percent, items done/total, elapsed time against the budget, the file being
processed and the output target), the queue in order, Recently Done (when it finished, how long it
took, where the output landed) and failures needing attention. Values update live every 1.5 s.

It is a view of the runner's own state and nothing more. It only answers GET requests, has no
controls and refuses non-localhost `Host` headers. Closing the tab never affects a job. Run with
`--no-gui` to turn it off.

## Install on Ultron (David)

Prerequisites: Python 3.11+ (`winget install Python.Python.3.12`), Ollama (with `OLLAMA_HOST`
left at `127.0.0.1`), and optionally ffmpeg (`winget install Gyan.FFmpeg`) for video frames.

```powershell
git clone https://github.com/realworlddigital/ultron-worker; cd ultron-worker
powershell -ExecutionPolicy Bypass -File scripts\install.ps1            # add -Transcribe for faster-whisper
```

This installs into `%ProgramData%\Ultron\venv` and creates `%ProgramData%\Ultron\config.toml` from
[`config.example.toml`](config.example.toml). It also creates an empty, user-only `runner.key` and
builds the desktop app, `%ProgramData%\Ultron\Ultron.exe`, with a Start Menu shortcut (**Ultron**).
Quit the app from its tray icon before re-running the installer by hand. Then:

1. **Key (David only).** Paste the `ultron-runner` key you issued into
   `%ProgramData%\Ultron\runner.key`. Nothing in this repo creates, stores or sends a key anywhere
   else, and the app never displays it.
2. **Config.** Set `backbone.url` and `runner.allowed_roots` (the photo, video and Dropbox folders).
3. **Models.** For example `ollama pull llava:13b`, `ollama pull nomic-embed-text`.
4. **Check.**
   `& "$env:ProgramData\Ultron\ultron-runner.cmd" --config "$env:ProgramData\Ultron\config.toml" --check`
   This confirms the key's identity, shows the queue, probes Ollama and lists the job kinds.
5. **Run it.** Start **Ultron** from the Start Menu and tick **Start Ultron when I sign in**.

Logs: `%ProgramData%\Ultron\state\ultron.log`. Uninstall: untick autostart, quit from the tray and
delete `%ProgramData%\Ultron`.

## Desktop app

`Ultron.exe` is a pywebview window (the built-in WebView2, no Electron) around the runner. The app
starts the runner as its child and owns it:

- **Controls.** Start, Stop and Restart. Stop and Quit close the runner's stdin, so it stops at its
  next checkpoint exactly like Ctrl+C. A runner that crashes is restarted after 5, 15 and 45 s; if
  it still crashes, the app stops retrying and shows **will not start - see log** until you press
  Start.
- **Environment.** The runner and its `--check` get your full environment, with PATH refreshed from
  the registry (so software installed since you signed in is found) and the active venv's
  `Scripts` first, the install folder as working directory and the absolute `config.toml` path.
  Their stdout/stderr is captured: a runner that dies before opening `ultron.log` still shows its
  error in the log pane (as `[runner output]` lines), and it is kept in
  `state\ultron-runner.output.log`.
- **The log file can't stop the runner.** `ultron.log` is opened (and tailed by the log pane)
  with read, write and delete sharing, so no reader blocks the writer or its rotation. If the
  runner still can't open it, it retries briefly, then logs to stderr only (shown in the log pane
  as `[no log file]` lines, with the reason) and keeps running. `--check` doesn't write it.
- **One runner per state dir.** A running runner holds a lock on `state\runner.pid` (the OS frees
  it when the runner exits, even if killed). Start first checks for a live runner (lock held or
  status GUI answering) and reports it as **outside app** with its pid instead of starting a
  rival; a second runner started by hand exits with code 4 and says which pid holds the lock.
- **Health lights.** Backbone identity, queue and Ollama, from the runner's own `--check`. They
  refresh after every start, every 10 minutes, and when you press **Re-check**. If the check itself
  can't run (won't start, crashes before reporting, times out), the three lights are replaced by
  one **Check unavailable** light with the reason: that is one problem, not three failures.
- **Status and log.** The existing status GUI (unchanged) in the middle, and a live tail of
  `ultron.log` below it.
- **Wake lock.** While the runner reports a current job, the app holds a Windows wake lock
  (`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`; the display may still sleep)
  and releases it when idle, so an overnight ingest isn't lost to idle sleep. Closing the lid or
  the power button still sleep; the job budget doesn't count that time.
- **Tray.** Closing the window hides it to the tray and the runner keeps working. The tray menu has
  Open, Restart runner and Quit (stops the runner cleanly). Launching Ultron again just brings the
  window back.
- **Autostart.** The toggle writes `HKCU\...\CurrentVersion\Run\Ultron` (per user, no admin). It
  replaces the old **Ultron Runner** Scheduled Task, which the installer now removes if present
  (`-RegisterTask` is retired).
- **Updates.** On launch and on **Check for updates**, the app compares the installed commit with
  `origin/main` and shows both short SHAs and subjects. git is found on PATH, then on the
  registry's current PATH, then in `Program Files\Git`, `%LOCALAPPDATA%\Programs\Git`, winget
  (`%LOCALAPPDATA%\Microsoft\WinGet\Links` and `Packages\Git.Git_*`), Scoop, Chocolatey
  (`%ProgramData%\chocolatey\bin`), GitHub Desktop's bundled git and Visual Studio's. An
  install recorded without its commit takes the clone's HEAD (read from `.git` if git can't run). Nothing updates without your click. If a job
  is running, **Update** waits for it to finish (or **Update now**: checkpointing resumes the job).
  It fast-forwards the checkout, builds a fresh venv in `venvs\<sha>`, and runs `--check` there.
  Only then does it stop the runner and restart the app on the new install, which shows
  **Updated to &lt;sha&gt;**. If any step fails, the previous install keeps running untouched.

The app serves its control page on a random loopback port. Its controls need a per-launch token, and
it proxies only GETs to the read-only status GUI. `install.json` records the repo, active venv and
installed commit. `ultron-runner.cmd` always points at the active venv.

**Acceptance run.** File a `vision_tag` card on a small folder. Within one poll it shows in the app
as the current job, with live progress and the file being processed. Press **Stop** partway, press
**Start**, and it resumes with no duplicate lines in `tags.jsonl`. When it finishes, the card is
`done` with an `ultron.completed` event and the job shows in Recently Done with its output path.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .
```

`contract/test_gilfoyle_ignores_local_jobs.py` runs inside a Gilfoyle checkout
(`cp contract/test_gilfoyle_ignores_local_jobs.py ../gilfoyle/tests/ && cd ../gilfoyle && pytest -q tests/test_gilfoyle_ignores_local_jobs.py`).
