"""The polling core: claim the oldest runner card, run it to a checkpointed finish, report.

Outbound only. Each tick lists the runner's own cards (in_progress first, to resume, then
dispatched, oldest first), keeps the GUI queue current, and starts at most one job. A job
runs item by item, committing each item's result before the next; progress events go out at
``progress_seconds``. The job itself never needs the network: if the backbone is down mid-job
the job carries on and its completion is reported on the next tick that reaches it.

Failures are bounded: retryable ones (Ollama down, unexpected errors) get ``max_attempts``
tries, then the card goes to needs_david with the error; non-retryable ones (bad spec, budget
spent, path outside allowed_roots) go there at once.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import __version__, jobs
from .backbone import Backbone, BackboneError, BackboneUnavailable, ScopeViolation
from .config import Config
from .jobs.base import ItemFailed, JobContext, JobError, JobSpec, JobStopped, WorkItem
from .ollama import Ollama, OllamaModelMissing, OllamaUnavailable
from .state import StatusBoard, Store, now_iso

log = logging.getLogger("ultron")
# Consecutive item failures, with no success yet in this run, that mean "the job is broken"
# (model missing, every file unreadable) rather than "this file is bad".
BROKEN_AFTER = 5
WATCH_STEP = 5.0  # sleep-watch granularity, seconds
BUDGET_GRACE = WATCH_STEP + 1  # lets the watch report a just-ended suspend first


class _Stopped(Exception):
    """Runner shutting down mid-job: leave the card in_progress, keep the checkpoint."""


class Runner:
    def __init__(
        self,
        cfg: Config,
        *,
        backbone: Backbone | None = None,
        ollama: Ollama | None = None,
        store: Store | None = None,
        background: bool = True,
    ) -> None:
        self.cfg = cfg
        self.bb = backbone or Backbone(cfg)
        self.ollama = ollama or Ollama(cfg.ollama_url)
        self.store = store or Store(cfg.db_path)
        self.board = StatusBoard(
            self.store,
            actor=cfg.actor,
            host=cfg.host_name,
            version=__version__,
            poll_seconds=cfg.poll_seconds,
        )
        self.background = background
        self.stop = threading.Event()
        self.poll_now = threading.Event()
        self._job_thread: threading.Thread | None = None
        self._verified = False
        jobs.load(cfg.job_modules)

    # ---- one poll -------------------------------------------------------------
    def tick(self) -> None:
        self.board.set(last_poll_at=now_iso())
        try:
            if not self._verified:
                self._verify_identity()
            in_progress = self.bb.cards("in_progress")
            dispatched = self.bb.cards("dispatched")
        except BackboneUnavailable as exc:
            self.board.contact(False, f"backbone unreachable: {exc}")
            log.warning("poll: backbone unreachable: %s", exc)
            return
        except BackboneError as exc:
            self.board.contact(False, f"backbone refused the runner ({exc.status}): {exc.detail}")
            log.error("poll: %s", exc)
            return
        self.board.contact(True)
        busy = self._job_thread is not None and self._job_thread.is_alive()
        current = (self.board.current() or {}).get("task_id")
        runnable = [t for t in in_progress if self._resumable(t)] + dispatched
        self.board.set_queue(
            [_queue_row(t, self.store.job(t["id"])) for t in runnable if t["id"] != current]
        )
        if busy or not runnable or self.stop.is_set():
            return
        card = runnable[0]
        fresh = card.get("status") == "dispatched"
        if self.background:
            self._job_thread = threading.Thread(
                target=self._execute_safely,
                args=(card, fresh),
                name=f"job-{card['id']}",
                daemon=True,
            )
            self._job_thread.start()
        else:
            self._execute_safely(card, fresh)

    def _verify_identity(self) -> None:
        me = self.bb.me()
        actor = (me.get("actor") or {}) if isinstance(me, dict) else {}
        if actor.get("id") != self.cfg.actor:
            raise BackboneError(
                403,
                f"key belongs to {actor.get('id')!r}, not "
                f"{self.cfg.actor!r}: the runner uses only its own actor",
            )
        self._verified = True

    def _resumable(self, task: dict[str, Any]) -> bool:
        job = self.store.job(task["id"])
        return job is None or job["status"] != "needs_david"

    # ---- one job --------------------------------------------------------------
    def _execute_safely(self, card: dict[str, Any], fresh: bool) -> None:
        try:
            self.execute(card, fresh=fresh)
        except Exception:  # noqa: BLE001 (a runner bug must never kill the poll loop)
            log.exception("job %s crashed the executor", card.get("id"))
        finally:
            self.board.set_current(None)
            self.poll_now.set()

    def execute(self, card: dict[str, Any], *, fresh: bool) -> None:
        tid = card["id"]
        data = card.get("data") or {}
        title = str(data.get("title") or tid)
        prior = self.store.job(tid)
        if not fresh and prior and prior["status"] in ("finished", "failing"):
            self._report(tid, prior)  # finished offline: only the report is left to do
            return
        if fresh:
            try:
                self.bb.transition(tid, "in_progress", note="Picked up by Ultron (local-ultron).")
            except BackboneError as exc:
                log.warning("claim of %s refused (%s): leaving it", tid, exc)
                return
        job_spec = data.get("job") if isinstance(data.get("job"), dict) else {}
        job = self.store.start_job(
            tid,
            title=title,
            kind=str(job_spec.get("kind") or "?"),
            model=str(job_spec.get("model") or "?"),
            output=str(job_spec.get("output") or ""),
            spec=json.dumps(
                {k: job_spec.get(k) for k in ("kind", "model", "params")},
                sort_keys=True,
                default=str,
            ),
            fresh_dispatch=fresh,
        )
        self._safe_event(
            "claimed" if fresh else "resumed",
            tid,
            f"Ultron {'picked up' if fresh else 'resumed'} {title}",
            attempt=job["attempts"] + 1,
            items_done=job["done"],
        )
        try:
            self._run(tid, title, data, job)
        except (_Stopped, JobStopped):
            log.info("job %s paused for shutdown at %s items", tid, self.store.job(tid)["done"])
            self.store.update_job(tid, current_item=None)
            return
        except JobError as exc:
            self._fail(tid, str(exc), retryable=exc.retryable)
        except OllamaModelMissing as exc:
            self._fail(tid, str(exc), retryable=False)
        except OllamaUnavailable as exc:
            self._fail(tid, str(exc), retryable=True)
        except ScopeViolation as exc:
            self._fail(tid, f"scope violation (runner bug): {exc}", retryable=False)
        except Exception as exc:  # noqa: BLE001 (unknown errors: bounded retries, then David)
            log.exception("job %s failed", tid)
            self._fail(tid, f"{type(exc).__name__}: {exc}", retryable=True)
        job = self.store.job(tid)
        if job and job["status"] in ("finished", "failing"):
            self._report(tid, job)

    def _run(self, tid: str, title: str, data: dict[str, Any], job: dict[str, Any]) -> None:
        spec = JobSpec.from_card(data)
        kind = jobs.get(spec.kind)
        if kind is None:
            raise JobError(f"unknown job kind {spec.kind!r}; this runner has {jobs.kinds()}")
        work_dir = self.cfg.state_dir / "jobs" / tid
        work_dir.mkdir(parents=True, exist_ok=True)
        ctx = JobContext(
            task_id=tid,
            ollama=self.ollama,
            allowed_roots=self.cfg.allowed_roots,
            work_dir=work_dir,
            stop=self.stop,
        )
        kind.validate(spec, ctx)
        items = kind.plan(spec, ctx)
        if not items:
            raise JobError("the job found nothing to process in its inputs")
        results = self.store.item_results(tid)
        done = sum(1 for it in items if it.key in results)
        self.store.update_job(tid, total=len(items), done=done)
        budget_s = spec.budget_minutes * 60
        self.board.set_current(
            {
                "task_id": tid,
                "title": title,
                "kind": spec.kind,
                "model": spec.model,
                "output": spec.output,
                "done": done,
                "total": len(items),
                "elapsed_s": round(job["active_seconds"]),
                "budget_s": budget_s,
                "current_item": None,
                "started_at": job["started_at"],
                "attempt": job["attempts"] + 1,
                "resumed_from": done or None,
            }
        )
        clock = _ActiveClock(self.board)
        active = job["active_seconds"]
        last_progress = 0.0
        consecutive_failures, successes = 0, 0
        self._progress(tid, done, len(items), None, active)
        try:
            for item in items:
                if item.key in results:
                    continue
                if self.stop.is_set():
                    raise _Stopped
                if active > budget_s and BUDGET_GRACE:
                    self.stop.wait(BUDGET_GRACE)
                    active = self.store.add_active(tid, clock.lap())
                if active > budget_s:
                    raise JobError(
                        f"wall-clock budget of {spec.budget_minutes:g} min spent after "
                        f"{done}/{len(items)} items; progress is checkpointed - raise "
                        f"data.job.budget_minutes and re-dispatch to resume"
                    )
                self.board.update_current(current_item=item.label)
                self.store.update_job(tid, current_item=item.label)
                try:
                    result = kind.run_item(item, spec, ctx)
                    consecutive_failures, successes = 0, successes + 1
                except ItemFailed as exc:
                    consecutive_failures += 1
                    if successes == 0 and consecutive_failures >= BROKEN_AFTER:
                        raise JobError(
                            f"first {BROKEN_AFTER} items all failed; last: {exc}", retryable=False
                        ) from exc
                    result = {"path": item.payload.get("path", item.label), "skipped": str(exc)}
                done += 1
                results[item.key] = result
                self.store.checkpoint(tid, item.key, result, done=done, current_item=item.label)
                active = self.store.add_active(tid, clock.lap())
                self.board.update_current(done=done, elapsed_s=round(active))
                if time.monotonic() - last_progress >= self.cfg.progress_seconds:
                    last_progress = time.monotonic()
                    self._progress(tid, done, len(items), item.label, active)
            self.board.update_current(current_item="writing output")
            ordered: list[tuple[WorkItem, dict[str, Any]]] = [(it, results[it.key]) for it in items]
            output = kind.finalize(spec, ordered, ctx)
            active = self.store.add_active(tid, clock.lap())
            self._progress(tid, done, len(items), None, active)
            self.store.update_job(
                tid,
                status="finished",
                outputs=output.paths,
                summary=output.summary,
                current_item=None,
                finished_at=now_iso(),
                error=None,
            )
        finally:
            self.store.add_active(tid, clock.lap())

    def _progress(self, tid: str, done: int, total: int, item: str | None, active: float) -> None:
        pct = round(100.0 * done / total, 1) if total else 0.0
        self._safe_event(
            "progress",
            tid,
            f"Ultron {tid}: {done}/{total} ({pct}%)",
            done=done,
            total=total,
            percent=pct,
            current_item=item,
            elapsed_s=round(active),
        )

    def _fail(self, tid: str, error: str, *, retryable: bool) -> None:
        job = self.store.job(tid) or {"attempts": 0}
        attempts = int(job["attempts"]) + 1
        if retryable and attempts < self.cfg.max_attempts:
            self.store.update_job(
                tid, status="retry_wait", attempts=attempts, error=error, current_item=None
            )
            self._safe_event(
                "retry",
                tid,
                f"Ultron will retry {tid} ({attempts}/{self.cfg.max_attempts}): {error}",
                severity="notable",
                attempt=attempts,
                max_attempts=self.cfg.max_attempts,
                error=error,
            )
            log.warning("job %s attempt %s failed, will retry: %s", tid, attempts, error)
            return
        self.store.update_job(
            tid,
            status="failing",
            attempts=attempts,
            error=error,
            current_item=None,
            finished_at=now_iso(),
        )
        log.error("job %s needs David: %s", tid, error)

    def _report(self, tid: str, job: dict[str, Any]) -> None:
        """Deliver a finished/failed job to the card. Safe to repeat: a report cut off by the
        network is redone on the next tick (the job itself is never rerun)."""
        if job["status"] == "finished":
            where = ", ".join(job["outputs"]) or "(no output)"
            minutes = job["active_seconds"] / 60
            final = "done"
            calls = [
                lambda: self.bb.event(
                    "completed",
                    tid,
                    f"Ultron finished {job['title']}: {job['summary']}",
                    outputs=job["outputs"],
                    summary=job["summary"],
                    items=job["total"],
                    duration_s=round(job["active_seconds"]),
                ),
                lambda: self.bb.note(
                    tid,
                    f"Ultron finished: {job['summary']} in {minutes:.1f} min "
                    f"({job['kind']}, {job['model']}). Output: {where}",
                ),
                lambda: self.bb.transition(tid, "done", note=f"Output: {where}"),
            ]
        else:
            final = "needs_david"
            calls = [
                lambda: self.bb.event(
                    "failed",
                    tid,
                    f"Ultron needs David on {job['title']}: {job['error']}",
                    severity="notable",
                    error=job["error"],
                    attempts=job["attempts"],
                    items_done=job["done"],
                    items_total=job["total"],
                ),
                lambda: self.bb.note(
                    tid,
                    f"Ultron stopped after {job['attempts']} attempt(s), "
                    f"{job['done']}/{job['total']} items checkpointed: "
                    f"{job['error']}\nRe-dispatch the card to resume from the "
                    f"checkpoint.",
                    kind="action",
                ),
                lambda: self.bb.transition(tid, "needs_david", note=job["error"][:500]),
            ]
        try:
            for step, call in enumerate(calls):
                if step < int(job.get("report_step") or 0):
                    continue  # delivered before the network dropped: never post it twice
                call()
                self.store.update_job(tid, report_step=step + 1)
            self.store.update_job(tid, status=final)
        except BackboneUnavailable as exc:
            log.warning("report for %s deferred (backbone unreachable): %s", tid, exc)
            self.board.contact(False, f"backbone unreachable: {exc}")
        except BackboneError as exc:
            log.error("report for %s refused: %s", tid, exc)

    def _safe_event(self, action: str, tid: str, summary: str, **data: Any) -> None:
        try:
            self.bb.event(action, tid, summary, **data)
        except BackboneUnavailable as exc:
            self.board.contact(False, f"backbone unreachable: {exc}")
        except BackboneError as exc:
            log.warning("event ultron.%s for %s refused: %s", action, tid, exc)

    # ---- heartbeat + sleep watch ----------------------------------------------
    def heartbeat(self) -> None:
        cur = self.board.current() or {}
        try:
            self.bb.status(
                ok=True,
                state=self.board.activity(),
                host=self.cfg.host_name,
                detail={
                    "version": __version__,
                    "task_id": cur.get("task_id"),
                    "done": cur.get("done"),
                    "total": cur.get("total"),
                    "queue": len(self.board.snapshot()["queue"]),
                },
            )
            self.board.contact(True)
            self.board.set(last_heartbeat_at=now_iso())
        except (BackboneUnavailable, BackboneError) as exc:
            self.board.contact(False, f"heartbeat failed: {exc}")

    def _watch(self) -> None:
        last_wall, last_beat = time.time(), 0.0
        while not self.stop.wait(WATCH_STEP):
            now = time.time()
            gap = now - last_wall - WATCH_STEP
            last_wall = now
            if gap > self.cfg.sleep_gap_seconds:
                log.info("woke after ~%.0f s asleep", gap)
                self.board.slept(gap)
                last_beat = 0.0
                self.poll_now.set()  # cards filed while asleep start on the first poll
            if time.monotonic() - last_beat >= self.cfg.heartbeat_seconds:
                last_beat = time.monotonic()
                self.heartbeat()

    def run_forever(self) -> None:
        watcher = threading.Thread(target=self._watch, name="heartbeat", daemon=True)
        watcher.start()
        while not self.stop.is_set():
            self.poll_now.clear()
            self.tick()
            self.poll_now.wait(self.cfg.poll_seconds)
        if self._job_thread is not None:
            self._job_thread.join(timeout=60)
        watcher.join(timeout=WATCH_STEP + 1)


class _ActiveClock:
    """Wall time minus detected suspend time: a job's budget never burns while Ultron sleeps."""

    def __init__(self, board: StatusBoard) -> None:
        self.board = board
        self._wall, self._slept = time.time(), board.sleep_seconds_total

    def lap(self) -> float:
        """Active seconds since the last lap. Negative when the sleep watch reports a suspend
        that an earlier lap already counted as wall time, so the total corrects itself."""
        wall, slept = time.time(), self.board.sleep_seconds_total
        delta = (wall - self._wall) - (slept - self._slept)
        self._wall, self._slept = wall, slept
        return delta


def _queue_row(task: dict[str, Any], job: dict[str, Any] | None) -> dict[str, Any]:
    data = task.get("data") or {}
    spec = data.get("job") if isinstance(data.get("job"), dict) else {}
    return {
        "task_id": task["id"],
        "title": data.get("title") or task["id"],
        "kind": spec.get("kind"),
        "model": spec.get("model"),
        "filed_at": task.get("created_at"),
        "status": task.get("status"),
        "waiting": (
            "retry" if job and job["status"] == "retry_wait" else "resume" if job else "new"
        ),
        "items_done": job["done"] if job else 0,
    }
