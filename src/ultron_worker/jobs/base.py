"""The contract every job kind implements, and the helpers they share."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ollama import Ollama

KEY_MAX = 1000


class JobStopped(Exception):
    """The runner is shutting down: the job pauses at its last checkpoint (not a failure)."""


class JobError(RuntimeError):
    """The job can't go on. ``retryable`` failures get bounded retries, the rest go straight
    to needs_david (a bad spec won't fix itself)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ItemFailed(RuntimeError):
    """One work item couldn't be processed (corrupt file, unsupported format...). Recorded in
    the output as skipped with its reason; the job carries on."""


@dataclass(frozen=True)
class JobSpec:
    kind: str
    inputs: tuple[str, ...]
    model: str
    output: str
    budget_minutes: float
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_card(cls, data: dict[str, Any]) -> JobSpec:
        job = data.get("job")
        if not isinstance(job, dict):
            raise JobError("card data.job is missing: it carries the job spec")
        inputs = job.get("inputs")
        if isinstance(inputs, str):
            inputs = [inputs]
        missing = [k for k in ("kind", "model", "output") if not job.get(k)]
        if not inputs:
            missing.append("inputs")
        if missing:
            raise JobError(f"card data.job is missing {', '.join(missing)}")
        try:
            budget = float(job.get("budget_minutes"))
        except (TypeError, ValueError) as exc:
            raise JobError(
                "card data.job.budget_minutes is required (a number of minutes)"
            ) from exc
        if budget <= 0:
            raise JobError("card data.job.budget_minutes must be positive")
        params = job.get("params") or {}
        if not isinstance(params, dict):
            raise JobError("card data.job.params must be an object")
        return cls(
            kind=str(job["kind"]),
            inputs=tuple(str(i) for i in inputs),
            model=str(job["model"]),
            output=str(job["output"]),
            budget_minutes=budget,
            params=params,
        )


@dataclass(frozen=True)
class WorkItem:
    key: str  # stable across restarts: the checkpoint key
    label: str  # what the GUI shows as "processing now"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Output:
    paths: list[str]
    summary: str


@dataclass
class JobContext:
    task_id: str
    ollama: Ollama
    allowed_roots: tuple[Path, ...]
    work_dir: Path
    stop: threading.Event = field(default_factory=threading.Event)
    cache: dict[str, Any] = field(default_factory=dict)

    def allowed(self, path: str | os.PathLike[str], *, what: str) -> Path:
        """Resolve ``path`` and refuse it unless it sits under one of the allowed roots."""
        p = Path(os.path.expandvars(str(path))).expanduser()
        resolved = p.resolve(strict=False)
        norm = os.path.normcase(str(resolved))
        for root in self.allowed_roots:
            r = os.path.normcase(str(root.expanduser().resolve(strict=False)))
            if norm == r or norm.startswith(r.rstrip("\\/") + os.sep):
                return resolved
        raise JobError(f"{what} {str(p)!r} is outside runner.allowed_roots")


class JobKind:
    """Subclass, set ``name``, implement :meth:`plan`, :meth:`run_item`, :meth:`finalize`."""

    name = ""
    needs_model = True

    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        for i in spec.inputs:
            if not ctx.allowed(i, what="input").exists():
                raise JobError(f"input {i!r} does not exist on this machine")
        ctx.allowed(spec.output, what="output target")

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        raise NotImplementedError

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        raise NotImplementedError

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        raise NotImplementedError


# ---- helpers shared by the kinds ---------------------------------------------
def iter_files(
    inputs: Iterable[str],
    ctx: JobContext,
    exts: set[str],
    *,
    recursive: bool = True,
    exclude: Iterable[Path] = (),
) -> list[Path]:
    """Every file under ``inputs`` with one of ``exts`` (resolved, inside the allowed roots,
    not in ``exclude``), sorted, de-duplicated."""
    skip = {os.path.normcase(str(Path(e).resolve(strict=False))) for e in exclude}
    seen: dict[str, Path] = {}
    for raw in inputs:
        p = ctx.allowed(raw, what="input")
        if p.is_file():
            candidates: Iterable[Path] = [p]
        elif p.is_dir():
            candidates = p.rglob("*") if recursive else p.glob("*")
        else:
            raise JobError(f"input {raw!r} does not exist on this machine")
        for f in candidates:
            if not (f.is_file() and f.suffix.lower() in exts):
                continue
            try:
                real = ctx.allowed(f, what="input")  # a link out of the allowed roots is skipped
            except JobError:
                continue
            key = os.path.normcase(str(real))
            if key not in skip:
                seen.setdefault(key, real)
    return [seen[k] for k in sorted(seen)]


def file_key(path: Path) -> str:
    return str(path)[:KEY_MAX]


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + rename, so readers (and a crash) only ever
    see the old or the new whole file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_records(path: Path, header: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """``.jsonl`` -> one record per line; anything else -> one JSON document."""
    if path.suffix.lower() == ".jsonl":
        atomic_write_text(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    else:
        atomic_write_text(
            path, json.dumps({**header, "items": records}, indent=2, ensure_ascii=False) + "\n"
        )
