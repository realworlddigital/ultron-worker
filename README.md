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

## How a job runs

1. **Poll.** Every `poll_seconds` (60 by default) the runner lists its `in_progress` cards first
   (to resume them) and then its `dispatched` cards, oldest first. It claims one with
   `dispatched → in_progress` and posts `ultron.claimed`.
2. **Checkpoint.** Each finished item is committed to SQLite (`state_dir\state.db`, WAL,
   synchronous=FULL) in the same transaction as the job's progress. If the runner is killed,
   rebooted or the laptop sleeps, it picks up after the last committed item (`ultron.resumed`) and
   never runs a finished item through the model again.
3. **Progress.** `ultron.progress` events (done/total, current file) go out at most every
   `progress_seconds`. A heartbeat goes to `system/status/ultron-runner` (`idle` / `working` /
   `paused-by-sleep`).
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
  next checkpoint exactly like Ctrl+C. A runner that crashes is restarted with a backoff.
- **Health lights.** Backbone identity, queue and Ollama, from the runner's own `--check`. They
  refresh after every start, every 10 minutes, and when you press **Run --check**.
- **Status and log.** The existing status GUI (unchanged) in the middle, and a live tail of
  `ultron.log` below it.
- **Tray.** Closing the window hides it to the tray and the runner keeps working. The tray menu has
  Open, Restart runner and Quit (stops the runner cleanly). Launching Ultron again just brings the
  window back.
- **Autostart.** The toggle writes `HKCU\...\CurrentVersion\Run\Ultron` (per user, no admin). It
  replaces the old **Ultron Runner** Scheduled Task, which the installer now removes if present
  (`-RegisterTask` is retired).
- **Updates.** On launch and on **Check for updates**, the app compares the installed commit with
  `origin/main` and shows both short SHAs and subjects. Nothing updates without your click. If a job
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
