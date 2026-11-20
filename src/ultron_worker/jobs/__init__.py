"""Job-kind registry. The polling core only ever calls :func:`get`; adding a kind (ComfyUI,
media generation, ...) is a new module that calls :func:`register`, loaded either from
``runner.job_modules`` in config.toml or from the ``ultron_worker.jobs`` entry-point group —
no change to the core.
"""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points

from .base import ItemRetry, JobContext, JobError, JobKind, JobSpec, Output, WorkItem

_REGISTRY: dict[str, JobKind] = {}
BUILTIN = (
    "ultron_worker.jobs.vision",
    "ultron_worker.jobs.text",
    "ultron_worker.jobs.embeddings",
    "ultron_worker.jobs.transcribe",
)


def register(kind: JobKind) -> JobKind:
    if not kind.name or kind.name in _REGISTRY and _REGISTRY[kind.name] is not kind:
        raise ValueError(f"job kind {kind.name!r} is already registered")
    _REGISTRY[kind.name] = kind
    return kind


def get(name: str) -> JobKind | None:
    return _REGISTRY.get(name)


def kinds() -> list[str]:
    return sorted(_REGISTRY)


def load(extra_modules: tuple[str, ...] | list[str] = ()) -> list[str]:
    for mod in (*BUILTIN, *extra_modules):
        importlib.import_module(mod)
    for ep in entry_points(group="ultron_worker.jobs"):
        ep.load()
    return kinds()


__all__ = [
    "ItemRetry",
    "JobContext",
    "JobError",
    "JobKind",
    "JobSpec",
    "Output",
    "WorkItem",
    "get",
    "kinds",
    "load",
    "register",
]
