"""The polling core: claim the oldest runner card, run it to a checkpointed finish, report.

Outbound only. Each tick lists the runner's own cards (in_progress first, to resume, then
dispatched, oldest first), keeps the GUI queue current, and starts at most one job. A job
runs item by item, committing each item's result before the next; progress events go out at
``progress_seconds``. The job itself never needs the network: if the backbone is down mid-job
the job carries on and its completion is reported on the next tick that reaches it. The job
thread never waits on the backbone between items: its events go to a small in-memory
:class:`Outbox` that the poll loop (and a sender thread) flush with short timeouts.

Every finished item logs one INFO line with where its time went (stat, frames, model calls
incl. a repair re-ask, checkpoint/post), and the phases ride along in its checkpoint row.

Failures are bounded: retryable ones (Ollama down, unexpected errors, items whose model output
stayed unusable - only those items are re-run) get ``max_attempts`` tries, then the card goes to
needs_david with the error; non-retryable ones (bad spec, budget spent, path outside
allowed_roots) go there at once.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__, jobs
from .backbone import Backbone, BackboneError, BackboneUnavailable, ScopeViolation
from .config import Config
from .feeds import FeedRunner
from .inbox import InboxWatcher
from .jobs.base import (
    ItemFailed,
    ItemRetry,
    ItemTiming,
    JobContext,
    JobError,
    JobSpec,
    JobStopped,
    TimedOllama,
    WorkItem,
    sweep_scratch,
)
from .ollama import Ollama, OllamaModelMissing, OllamaUnavailable
from .state import StatusBoard, Store, now_iso

log = logging.getLogger("ultron")
# Consecutive item failures, with no success yet in this run, that mean "the job is broken"
# (model missing, every file unreadable) rather than "this file is bad".
BROKEN_AFTER = 5
WATCH_STEP = 5.0  # sleep-watch granularity, seconds
BUDGET_GRACE = WATCH_STEP + 1  # lets the watch report a just-ended suspend first
OUTBOX_LIMIT = 200  # queued job events kept while the backbone is unreachable (oldest dropped)
# Flushes (each after a good poll) an event may fail before it's dropped, so one event the
# backbone keeps failing can't hold the queue, or the card reports waiting on it, forever.
EVENT_ATTEMPTS = 5
ERROR_CHARS = 200  # of a failed item's error in a progress summary (the event data has all of it)


@dataclass
class _Event:
    action: str
    tid: str
    summary: str
    severity: str = "routine"
    data: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0


class Outbox:
    """Job-thread events waiting for the backbone: bounded, and while the backbone is known
    down only the newest ``progress`` per card is kept (an older one says nothing the newer
    doesn't). The job thread only ever ``put``s; ``flush`` runs on the poll loop or the sender
    thread."""

    def __init__(self, limit: int = OUTBOX_LIMIT) -> None:
        self.limit = limit
        self.dropped = 0
        self._items: deque[_Event] = deque()
        self._lock = threading.Lock()
        self._flushing = threading.Lock()  # one flush at a time keeps events in order
        self.ready = threading.Event()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def pending(self, tid: str) -> int:
        with self._lock:
            return sum(1 for e in self._items if e.tid == tid)

    def put(self, event: _Event, *, coalesce: bool = False) -> None:
        with self._lock:
            if coalesce and event.action == "progress":
                stale = [e for e in self._items if e.action == "progress" and e.tid == event.tid]
                for e in stale:
                    self._items.remove(e)
            self._items.append(event)
            while len(self._items) > self.limit:
                self._items.popleft()
                self.dropped += 1
        self.ready.set()

    def flush(self, send: Any) -> int:
        """Send queued events oldest first; stop (keeping the rest) at the first
        :class:`BackboneUnavailable`, which propagates; an event that has failed that way
        ``EVENT_ATTEMPTS`` times is dropped. A refused event is logged and dropped.
        Returns how many were delivered."""
        sent = 0
        with self._flushing:
            while True:
                with self._lock:
                    if not self._items:
                        return sent
                    event = self._items[0]
                try:
                    send(event)
                    sent += 1
                except BackboneUnavailable:
                    event.attempts += 1
                    if event.attempts >= EVENT_ATTEMPTS:
                        log.warning(
                            "event ultron.%s for %s dropped after %s failed sends",
                            event.action,
                            event.tid,
                            event.attempts,
                        )
                        with self._lock:
                            if self._items and self._items[0] is event:
                                self._items.popleft()
                                self.dropped += 1
                    raise
                except BackboneError as exc:
                    log.warning("event ultron.%s for %s refused: %s", event.action, event.tid, exc)
                with self._lock:
                    if self._items and self._items[0] is event:
                        self._items.popleft()


class _Stopped(Exception):
    """Runner shutting down mid-job: leave the card in_progress, keep the checkpoint."""


def job_fingerprint(job_spec: dict[str, Any]) -> str:
    """What the checkpoints depend on; normalised like JobSpec.from_card (no params == {})."""
    return json.dumps(
        {
            "kind": job_spec.get("kind"),
            "model": job_spec.get("model"),
            "params": job_spec.get("params") or {},
        },
        sort_keys=True,
        default=str,
    )


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
        self.outbox = Outbox()
        self._backbone_down = False  # the last flush hit a dead backbone: wait for a good poll
        jobs.load(cfg.job_modules)
        self.inbox: InboxWatcher | None = None
        if cfg.media_inbox.enabled:
            self.inbox = InboxWatcher(
                cfg,
                self.bb,
                self.ollama,
                stop=self.stop,
                busy=lambda: self._job_thread is not None and self._job_thread.is_alive(),
            )
        self.feeds: FeedRunner | None = None
        if self.inbox is not None and cfg.api_feeds():
            self.feeds = FeedRunner(cfg, self.inbox)

    def sweep_scratch(self) -> int:
        """Startup only (no job running): drop frame scratch dirs orphaned by a hard kill."""
        removed = sweep_scratch(self.cfg.state_dir / "jobs")
        if removed:
            log.info("startup: removed %s stale scratch dir(s) left by an earlier kill", removed)
        return removed

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
        self._backbone_down = False
        self.flush_outbox()
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
            if not self._backbone_down:
                self.flush_outbox()

    def flush_outbox(self) -> None:
        """Deliver the job's queued events (never called from the job thread mid-job)."""
        try:
            self.outbox.flush(
                lambda e: self.bb.event(e.action, e.tid, e.summary, severity=e.severity, **e.data)
            )
        except BackboneUnavailable as exc:
            self._backbone_down = True
            self.board.contact(False, f"backbone unreachable: {exc}")
            log.warning("%s job event(s) queued (backbone unreachable): %s", len(self.outbox), exc)

    def _send_outbox(self) -> None:
        """Sender thread: flush soon after the job queues an event, unless the last flush
        found the backbone down (then the next good poll flushes)."""
        while not self.stop.is_set():
            self.outbox.ready.wait(self.cfg.poll_seconds)
            self.outbox.ready.clear()
            if not self.stop.is_set() and not self._backbone_down and len(self.outbox):
                self.flush_outbox()

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
            spec=job_fingerprint(job_spec),
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
        if kind.needs_model and not spec.model:
            raise JobError("card data.job is missing model")
        work_dir = self.cfg.state_dir / "jobs" / tid
        work_dir.mkdir(parents=True, exist_ok=True)
        planning = time.perf_counter()
        ctx = JobContext(
            task_id=tid,
            ollama=self.ollama,
            allowed_roots=self.cfg.allowed_roots,
            media_roots=self.cfg.roots(),
            work_dir=work_dir,
            stop=self.stop,
        )
        ctx.ollama = TimedOllama(self.ollama, ctx)
        kind.validate(spec, ctx)
        items = kind.plan(spec, ctx)
        if not items:
            raise JobError("the job found nothing to process in its inputs")
        results = self.store.item_results(tid)
        for key, seeded in kind.seed(spec, items, ctx).items():
            if not (key in results and kind.settled(results[key])):
                results[key] = seeded  # known done from outside the checkpoint: no model call
        done = sum(1 for it in items if it.key in results and kind.settled(results[it.key]))
        self.store.update_job(tid, total=len(items), done=done)
        log.info(
            "job %s: %s item(s) enumerated in %.1fs, %s already done",
            tid,
            len(items),
            time.perf_counter() - planning,
            done,
        )
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
                if item.key in results and kind.settled(results[item.key]):
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
                ctx.timing = ItemTiming()
                item_started, slept_before = time.perf_counter(), self.board.sleep_seconds_total
                try:
                    result = kind.run_item(item, spec, ctx)
                    consecutive_failures, successes = 0, successes + 1
                except ItemFailed as exc:
                    consecutive_failures += 1
                    retry_pass = isinstance(exc, ItemRetry) and done > 0  # not a broken job
                    if successes == 0 and consecutive_failures >= BROKEN_AFTER and not retry_pass:
                        raise JobError(
                            f"first {BROKEN_AFTER} items all failed; last: {exc}", retryable=False
                        ) from exc
                    what = "failed" if isinstance(exc, ItemRetry) else "skipped"
                    result = {"path": item.payload.get("path", item.label), what: str(exc)}
                if kind.settled(result):
                    done += 1
                results[item.key] = result
                timing = _phases(ctx.timing, time.perf_counter() - item_started)
                post_started = time.perf_counter()
                self.store.checkpoint(
                    tid, item.key, result, done=done, current_item=item.label, timing=timing
                )
                active = self.store.add_active(tid, clock.lap())
                self.board.update_current(done=done, elapsed_s=round(active))
                name, post = _item_name(item, ctx), time.perf_counter() - post_started
                if time.monotonic() - last_progress >= self.cfg.progress_seconds:
                    last_progress = time.monotonic()
                    last = (
                        _last_item(name, result, timing, post, ctx.timing.counts)
                        if ctx.timing.seconds
                        else None
                    )
                    self._progress(tid, done, len(items), item.label, active, last)
                log.info(
                    _item_line(
                        name,
                        result,
                        timing,
                        ctx.timing.calls,
                        post=post,
                        slept=self.board.sleep_seconds_total - slept_before,
                        counts=ctx.timing.counts,
                    )
                )
            self.board.update_current(current_item="writing output")
            ordered: list[tuple[WorkItem, dict[str, Any]]] = [(it, results[it.key]) for it in items]
            output = kind.finalize(spec, ordered, ctx)
            active = self.store.add_active(tid, clock.lap())
            self._progress(tid, done, len(items), None, active)
            failed = [(it, r) for it, r in ordered if not kind.settled(r)]
            if failed:
                first, why = failed[0][0].label, failed[0][1].get("failed", "unusable result")
                raise JobError(
                    f"{len(failed)} item(s) failed and are retried from the checkpoint on the "
                    f"next attempt (first: {first}: {why}); output so far, with them marked "
                    f"failed: {', '.join(output.paths) or '(none)'}",
                    retryable=True,
                )
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

    def _progress(
        self,
        tid: str,
        done: int,
        total: int,
        item: str | None,
        active: float,
        last: dict[str, Any] | None = None,
    ) -> None:
        """``last``: the just-finished item's outcome and phases (job kinds that time phases),
        appended after the count prefix that existing readers key on."""
        pct = round(100.0 * done / total, 1) if total else 0.0
        summary = f"Ultron {tid}: {done}/{total} ({pct}%)"
        extra: dict[str, Any] = {}
        if last is not None:
            summary += f" - last item {_last_item_text(last)}"
            extra["last_item"] = last
        self._safe_event(
            "progress",
            tid,
            summary,
            done=done,
            total=total,
            percent=pct,
            current_item=item,
            elapsed_s=round(active),
            **extra,
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
            headline, _, detail = (job["summary"] or "").partition("\n")
            used = job["kind"] if job["model"] in ("", "?") else f"{job['kind']}, {job['model']}"
            final = "done"
            calls = [
                lambda: self.bb.event(
                    "completed",
                    tid,
                    f"Ultron finished {job['title']}: {headline}",
                    outputs=job["outputs"],
                    summary=headline,
                    items=job["total"],
                    duration_s=round(job["active_seconds"]),
                ),
                lambda: self.bb.note(
                    tid,
                    f"Ultron finished: {headline} in {minutes:.1f} min ({used}). "
                    f"Output: {where}" + (f"\n{detail}" if detail else ""),
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
        if not self._backbone_down:  # else this poll's flush just failed: one try per poll
            self.flush_outbox()  # the job's own events go out before its completion
        if queued := self.outbox.pending(tid):
            log.warning("report for %s deferred: %s of its event(s) still queued", tid, queued)
            return
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

    def _safe_event(
        self, action: str, tid: str, summary: str, *, severity: str = "routine", **data: Any
    ) -> None:
        """Queue a job event: never a network call on the job thread (see :class:`Outbox`)."""
        self.outbox.put(_Event(action, tid, summary, severity, data), coalesce=self._backbone_down)

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
                    **(
                        {"inbox": {**self.inbox.last, "pending": self.inbox.last_counts}}
                        if self.inbox is not None
                        else {}
                    ),
                    **({"feeds": self.feeds.last} if self.feeds is not None else {}),
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
        sender = threading.Thread(target=self._send_outbox, name="outbox", daemon=True)
        sender.start()
        inbox = None
        if self.inbox is not None:
            inbox = threading.Thread(
                target=self.inbox.run_forever,
                args=(self.cfg.poll_seconds,),
                name="inbox",
                daemon=True,
            )
            inbox.start()
        feeds = None
        if self.feeds is not None:
            feeds = threading.Thread(target=self.feeds.run_forever, name="feeds", daemon=True)
            feeds.start()
        while not self.stop.is_set():
            self.poll_now.clear()
            self.tick()
            self.poll_now.wait(self.cfg.poll_seconds)
        if self._job_thread is not None:
            self._job_thread.join(timeout=60)
        watcher.join(timeout=WATCH_STEP + 1)
        self.outbox.ready.set()
        sender.join(timeout=WATCH_STEP + 1)
        if inbox is not None:
            inbox.join(timeout=60)
        if feeds is not None:
            feeds.join(timeout=60)


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


def _phases(timing: ItemTiming, total: float) -> dict[str, float]:
    """The item's phases plus ``other`` (time no phase claimed) and ``total``, rounded."""
    out = {name: round(sec, 3) for name, sec in timing.seconds.items()}
    out["other"] = round(max(0.0, total - sum(timing.seconds.values())), 3)
    out["total"] = round(total, 3)
    return out


def _item_name(item: WorkItem, ctx: JobContext) -> str:
    """``ROOT/rel/path`` for a file under a media root, else the item's label."""
    path = item.payload.get("path")
    found = ctx.media_roots.relativize(Path(path)) if path and ctx.media_roots else None
    return f"{found[0]}/{found[1]}" if found else item.label


def _outcome(result: dict[str, Any]) -> str:
    return "skipped" if "skipped" in result else "failed" if "failed" in result else "done"


def _last_item(
    name: str,
    result: dict[str, Any],
    timing: dict[str, float],
    post: float,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """The ``last_item`` payload of a progress event: the same phases (and counts) as the
    item's log line."""
    outcome = _outcome(result)
    phases = {k: v for k, v in timing.items() if k != "total"}
    phases["post"] = round(post, 3)
    last = {
        "name": name,
        "outcome": outcome,
        "phases": phases,
        "total_s": round(timing["total"] + post, 3),
        "error": None if outcome == "done" else str(result[outcome]),
    }
    if counts:
        last["counts"] = dict(counts)
    return last


def _last_item_text(last: dict[str, Any]) -> str:
    phases = ", ".join(
        [f"{k} {v:.1f}s" for k, v in last["phases"].items()]
        + [f"{k} {n}" for k, n in (last.get("counts") or {}).items()]
    )
    if last["outcome"] == "done":
        return f"{last['name']}: {phases}"
    error = " ".join(last["error"].split())
    error = error if len(error) <= ERROR_CHARS else error[: ERROR_CHARS - 3] + "..."
    return f"{last['name']} {last['outcome']} ({phases}): {error}"  # error last: cut first


def _item_line(
    name: str,
    result: dict[str, Any],
    timing: dict[str, float],
    calls: dict[str, int],
    *,
    post: float,
    slept: float,
    counts: dict[str, int] | None = None,
) -> str:
    outcome = _outcome(result)
    parts = []
    for phase, sec in timing.items():
        if phase == "total":
            continue
        n = calls.get(phase, 1)
        parts.append(f"{phase} {sec:.1f}s" + (f" x{n}" if n > 1 else ""))
    parts.append(f"post {post:.1f}s")
    parts.extend(f"{k} {n}" for k, n in (counts or {}).items())
    if slept > 0:
        parts.append(f"incl. ~{slept:.0f}s asleep")
    return f"item {name} {outcome} in {timing['total'] + post:.1f}s ({', '.join(parts)})"


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
