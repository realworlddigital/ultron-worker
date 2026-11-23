"""The media library's catalog: file stamps for index-seeded skipping, and the per-root TOC.

Every tag record carries ``size_bytes`` + ``mtime`` from the file's stat at tag time, so a later
job over the same folder can tell an unchanged file (done, no model call) from a changed one.

At each media root, ``_index/catalog.jsonl`` lists one entry per indexed folder::

    {"root": "GUILD-RAW-Media", "folder": "Guild Hall/2026-10-GH NATICK",
     "index": "Guild Hall/2026-10-GH NATICK/tags.jsonl", "files": 67, "tagged": 60,
     "skipped": 4, "failed": 3, "model": "muse-glimmer:30b", "updated_at": "<ISO UTC>"}

upserted on (root, folder), and ``_index/all-tags.jsonl`` holds every record of every listed
index, merged with :meth:`MediaRoots.merge` (root + relpath; tagged beats failed/skipped;
first-seen order).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .jobs.base import JobError, atomic_write_text
from .media import MediaRoots, load_index

CATALOG_DIR = "_index"
TOC_NAME = "catalog.jsonl"
ALL_TAGS_NAME = "all-tags.jsonl"
# Copies to FAT/exFAT (and some SMB servers) keep mtime only to 2 s.
MTIME_SLACK = 2.0
REPLACE_TRIES = 5


def file_stamp(path: str | os.PathLike[str]) -> dict[str, Any]:
    st = os.stat(path)
    return {"size_bytes": st.st_size, "mtime": round(st.st_mtime, 3)}


def has_stamp(record: dict[str, Any]) -> bool:
    size, mtime = record.get("size_bytes"), record.get("mtime")
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and isinstance(mtime, int | float)
        and not isinstance(mtime, bool)
    )


def stamp_matches(record: dict[str, Any], stamp: dict[str, Any]) -> bool:
    return (
        record["size_bytes"] == stamp["size_bytes"]
        and abs(float(record["mtime"]) - float(stamp["mtime"])) <= MTIME_SLACK
    )


def _write(path: Path, records: list[dict[str, Any]]) -> None:
    """Atomic jsonl write; retried while a reader that doesn't share delete holds the target
    open (Windows refuses the replace for a moment)."""
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    for attempt in range(REPLACE_TRIES):
        try:
            atomic_write_text(path, text)
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def _toc_key(roots: MediaRoots, entry: dict[str, Any]) -> tuple[str, str]:
    return roots.identity({"root": entry.get("root"), "relpath": entry.get("folder")})


def update_catalog(
    roots: MediaRoots,
    allowed: Callable[[str], Path],
    *,
    index_path: Path,
    folder_path: Path | None,
    counts: dict[str, int],
    model: str,
) -> str:
    """Upsert this folder's TOC entry and rebuild ``all-tags.jsonl`` at the index's media root.
    Returns a short note for the completion summary. Never raises for catalog trouble: the
    folder index is already written, and the next completion rebuilds the catalog anyway."""
    found = roots.relativize(index_path)
    if found is None:
        return "catalog not updated: the output is outside every media root"
    root, index_rel = found
    base = roots.resolve(root, "")
    try:
        cat_dir = allowed(str(Path(base) / CATALOG_DIR))
    except JobError as exc:
        return f"catalog not updated: {exc}"
    in_root = roots.relativize(folder_path) if folder_path is not None else None
    if in_root is not None and roots.lookup(in_root[0]) == roots.lookup(root):
        folder = in_root[1]
    else:
        folder = "/".join(index_rel.split("/")[:-1])
    entry = {
        "root": root,
        "folder": folder,
        "index": index_rel,
        **{k: counts[k] for k in ("files", "tagged", "skipped", "failed")},
        "model": model,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    toc_path = cat_dir / TOC_NAME
    try:
        toc = load_index(toc_path, roots) if toc_path.is_file() else []
    except (OSError, ValueError) as exc:
        return f"catalog not updated: {CATALOG_DIR}/{TOC_NAME} can't be read: {exc}"
    key = _toc_key(roots, entry)
    for i, old in enumerate(toc):
        if _toc_key(roots, old) == key:
            toc[i] = entry
            break
    else:
        toc.append(entry)
    passes, unread = [], []
    for e in toc:
        ref = f"{e.get('root')}/{e.get('index')}"
        where = (
            roots.resolve(e["root"], e["index"])
            if isinstance(e.get("root"), str) and isinstance(e.get("index"), str)
            else None
        )
        try:
            if where is None:
                raise ValueError("no such media root on this machine")
            passes.append(load_index(where, roots))
        except (OSError, ValueError):
            unread.append(ref)
    try:
        _write(toc_path, toc)
        _write(cat_dir / ALL_TAGS_NAME, roots.merge(*passes))
    except OSError as exc:
        return f"catalog not updated: {exc}"
    note = f"catalog {root}/{CATALOG_DIR} updated ({len(toc)} folder(s))"
    if unread:
        note += f"; skipped missing/unreadable index(es): {', '.join(unread)}"
    return note
