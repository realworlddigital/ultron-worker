"""folder_inventory: a model-free stat walk of media folders, diffed against the tag catalog.

Every media file (the extensions vision_tag reads) under the input folders becomes one record
``{"root", "rel", "size_bytes", "mtime", "media"}`` in the ``.jsonl`` output. No model is called.
When the walk is done it is compared with the library's tag indexes: the TOC at
``<root>/_index/catalog.jsonl`` lists one folder index per line (``{"root", "folder", "index",
...}``), and any record in those indexes counts a file as indexed. The result note lists, per
folder, how many files have no tag record; ``<output stem>.unindexed.json`` holds the full table.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..media import MediaRoots, is_absolute, load_index, read_index
from . import register
from .base import (
    JobContext,
    JobError,
    JobKind,
    JobSpec,
    JobStopped,
    Output,
    WorkItem,
    atomic_write_text,
    write_records,
)
from .vision import IMAGE_EXTS, VIDEO_EXTS

CATALOG = "_index/catalog.jsonl"
# NAS/OS housekeeping folders (Synology thumbnails and recycle bins, Windows and macOS system
# folders) plus the catalog's own _index: never footage.
SYSTEM_DIRS = frozenset(
    n.casefold()
    for n in (
        "_index", "@eaDir", "#recycle", "#snapshot", "$RECYCLE.BIN",
        "System Volume Information", ".Trashes", ".Spotlight-V100", ".fseventsd",
    )
)  # fmt: skip
NOTE_DETAIL_MAX = 3000  # the backbone keeps 4000 chars of a note; the headline needs the rest
WALK_CACHE = "folder_inventory.walk"
REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


class FolderInventory(JobKind):
    name = "folder_inventory"
    needs_model = False

    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        super().validate(spec, ctx)
        out = ctx.allowed(spec.output, what="output target")
        if out.suffix.lower() != ".jsonl" or out.is_dir():
            raise JobError(f"output target {str(out)!r} must be a .jsonl file")
        for raw in spec.inputs:
            p = ctx.allowed(raw, what="input")
            if not p.is_dir():
                raise JobError(f"input {raw!r} is not a folder")
            if ctx.media_roots.relativize(p) is None:
                raise JobError(
                    f"input {raw!r} is not under a named media root ([media_roots] or "
                    "runner.allowed_roots): its files would have no root + rel identity"
                )
        _group_depth(spec)
        _excluded(spec)

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        walk = _walk(spec, ctx)
        ctx.cache[WALK_CACHE] = walk
        return [
            WorkItem(
                key=f"{root}/{folder}",
                label=f"{root}/{folder}" if folder else root,
                payload={"root": root, "folder": folder},
            )
            for (root, folder) in sorted(walk["folders"], key=lambda k: (k[0], k[1].casefold()))
        ]

    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        walk = ctx.cache.get(WALK_CACHE) or {"folders": {}}
        files = walk["folders"].get((item.payload["root"], item.payload["folder"]), [])
        return {"root": item.payload["root"], "folder": item.payload["folder"], "files": files}

    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        out = ctx.allowed(spec.output, what="output target")
        walk = ctx.cache.get(WALK_CACHE)
        if walk is not None:  # this attempt's walk: a re-dispatch never reports stale folders
            records = [
                f
                for item, _ in results
                for f in walk["folders"].get((item.payload["root"], item.payload["folder"]), [])
            ]
        else:
            records = [f for _, r in results for f in r.get("files", [])]
        write_records(out, {}, records)
        roots = ctx.media_roots
        indexed: set[tuple[str, str]] = set()
        untagged: set[tuple[str, str]] = set()  # indexed, but only as failed/skipped
        catalog_notes: list[str] = []
        for root in sorted({r["root"] for r in records}):
            seen, attempted, notes = _catalog(root, ctx)
            indexed |= seen
            untagged |= attempted
            catalog_notes += notes
        depth = _group_depth(spec)
        folders: dict[tuple[str, str], dict[str, int]] = {}
        for rec in records:
            folder = rec["rel"].rsplit("/", 1)[0] if "/" in rec["rel"] else ""
            if depth is not None:
                folder = "/".join(folder.split("/")[:depth]) if folder else ""
            row = folders.setdefault((rec["root"], folder), {"files": 0, "unindexed": 0})
            row["files"] += 1
            key = roots.identity({"root": rec["root"], "relpath": rec["rel"]})
            if key not in indexed:
                row["unindexed"] += 1
        total_unindexed = sum(r["unindexed"] for r in folders.values())
        rows = [
            {"root": root, "folder": folder, **counts}
            for (root, folder), counts in sorted(
                folders.items(), key=lambda kv: (kv[0][0], kv[0][1].casefold())
            )
        ]
        pending = [r for r in rows if r["unindexed"]]
        untagged_n = sum(
            1
            for rec in records
            if roots.identity({"root": rec["root"], "relpath": rec["rel"]}) in untagged
        )
        sidecar = out.with_name(out.stem + ".unindexed.json")
        atomic_write_text(
            sidecar,
            json.dumps(
                {
                    "job": self.name,
                    "task_id": ctx.task_id,
                    "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "files": len(records),
                    "unindexed": total_unindexed,
                    "indexed_untagged": untagged_n,
                    "catalog": catalog_notes,
                    "unreadable": walk_problems(ctx),
                    "folders": pending,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        media = Counter(r["media"] for r in records)
        size_gb = sum(r["size_bytes"] for r in records) / 1e9
        headline = (
            f"{len(records)} media files ({media['video']} video, {media['image']} image, "
            f"{size_gb:.1f} GB) in {len(rows)} folders; {total_unindexed} unindexed in "
            f"{len(pending)} folders"
        )
        detail = catalog_notes + [f"Unindexed files: {total_unindexed} of {len(records)}"]
        if untagged_n:
            detail.append(f"{untagged_n} indexed file(s) have only a failed/skipped record")
        problems = walk_problems(ctx)
        if problems:
            detail.append(f"{len(problems)} unreadable folder(s) skipped (first: {problems[0]})")
        if pending:
            detail.append("Unindexed by folder (unindexed/files):")
            lines = [
                f"- {_join(r['root'], r['folder']).rstrip('/')}: {r['unindexed']}/{r['files']}"
                for r in pending
            ]
            detail += _fit(
                lines,
                NOTE_DETAIL_MAX - sum(len(d) + 1 for d in detail),
                sidecar,
            )
        return Output(paths=[str(out), str(sidecar)], summary="\n".join([headline, *detail]))


def walk_problems(ctx: JobContext) -> list[str]:
    return list((ctx.cache.get(WALK_CACHE) or {}).get("unreadable", []))


def _fit(lines: list[str], budget: int, sidecar: Path) -> list[str]:
    """As many folder lines as fit in ``budget`` chars, then a pointer to the full table."""
    if sum(len(line) + 1 for line in lines) <= budget:
        return lines
    more = f"- ... {len(lines)} more folder(s): full list in {sidecar}"
    kept: list[str] = []
    used = len(more) + 1
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return [*kept, f"- ... {len(lines) - len(kept)} more folder(s): full list in {sidecar}"]


def _group_depth(spec: JobSpec) -> int | None:
    raw = spec.params.get("group_depth")
    if raw is None:
        return None
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise JobError("card data.job.params.group_depth must be a whole number >= 1")
    return raw


def _excluded(spec: JobSpec) -> frozenset[str]:
    extra = spec.params.get("exclude") or []
    if isinstance(extra, str):
        extra = [extra]
    if not isinstance(extra, list) or not all(isinstance(e, str) and e for e in extra):
        raise JobError("card data.job.params.exclude must be a list of folder names")
    return SYSTEM_DIRS | {e.casefold() for e in extra}


def _reparse_point(entry: os.DirEntry[str]) -> bool:
    """A Windows junction (or other reparse point) passes ``is_dir(follow_symlinks=False)``
    and could lead out of the allowed roots: never descended, like a folder symlink."""
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & REPARSE_POINT)


def _walk(spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
    """Stat every media file under the inputs (os.scandir: on Windows/SMB the listing carries
    the size and mtime, so no extra round trip per file). Folder symlinks are not followed."""
    exts = IMAGE_EXTS | VIDEO_EXTS
    skip = _excluded(spec)
    roots: MediaRoots = ctx.media_roots
    folders: dict[tuple[str, str], list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    unreadable: list[str] = []
    for raw in spec.inputs:
        top = ctx.allowed(raw, what="input")
        if roots.relativize(top) is None:
            raise JobError(f"input {raw!r} is not under a named media root")
        stack = [str(top)]
        while stack:
            if ctx.stop.is_set():
                raise JobStopped
            path = stack.pop()
            # per folder, not per input: a nested media root takes over, as in vision_tag
            found = roots.relativize(path)
            if found is None:
                continue
            root, folder = found
            try:
                with os.scandir(path) as it:
                    entries = sorted(it, key=lambda e: e.name.casefold())
            except OSError as exc:
                unreadable.append(f"{path}: {exc.strerror or exc}")
                continue
            for entry in reversed(entries):
                rel = _join(folder, entry.name)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.casefold() not in skip and not _reparse_point(entry):
                            stack.append(entry.path)
                        continue
                    suffix = os.path.splitext(entry.name)[1].lower()
                    if suffix not in exts or not entry.is_file():
                        continue
                    if entry.is_symlink():
                        ctx.allowed(entry.path, what="input")  # a link out of the roots: skipped
                    st = entry.stat()
                except (OSError, JobError):
                    continue
                key = roots.identity({"root": root, "relpath": rel})
                if key in seen:
                    continue  # overlapping inputs
                seen.add(key)
                folders.setdefault((root, folder), []).append(
                    {
                        "root": root,
                        "rel": rel,
                        "size_bytes": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(
                            timespec="seconds"
                        ),
                        "media": "video" if suffix in VIDEO_EXTS else "image",
                    }
                )
    for files in folders.values():
        files.sort(key=lambda r: r["rel"].casefold())
    return {"folders": folders, "unreadable": unreadable}


def _catalog(
    root: str, ctx: JobContext
) -> tuple[set[tuple[str, str]], set[tuple[str, str]], list[str]]:
    """(identities with a tag record, those whose record is only failed/skipped, notes) from
    ``<root>/_index/catalog.jsonl`` and the folder indexes it lists."""
    roots: MediaRoots = ctx.media_roots
    where = roots.resolve(root, CATALOG)
    path = ctx.allowed(where, what="catalog") if where else None
    if path is None or not path.is_file():
        return set(), set(), [f"No catalog at {where} yet: every {root} file counts as unindexed"]
    try:
        toc = read_index(path)
    except (OSError, ValueError) as exc:
        raise JobError(f"catalog {str(path)!r} can't be read: {exc}") from exc
    passes: list[list[dict[str, Any]]] = []
    missing: list[str] = []
    for entry in toc:
        if roots.lookup(str(entry.get("root") or root)) != roots.lookup(root):
            continue
        ref = entry.get("index")
        if not isinstance(ref, str) or not ref:
            missing.append(f"{entry.get('folder')!r} (no index path)")
            continue
        target = ref if is_absolute(ref) else roots.resolve(root, ref)
        try:
            index = ctx.allowed(target, what="folder index")
            passes.append(load_index(index, roots))
        except (OSError, ValueError, JobError) as exc:
            missing.append(f"{ref} ({exc.__class__.__name__}: {exc})")
    merged = roots.merge(*passes)
    seen = {roots.identity(r) for r in merged}
    attempted = {roots.identity(r) for r in merged if "failed" in r or "skipped" in r}
    notes = [f"Catalog {path}: {len(passes)} folder index(es) read"]
    if missing:
        notes.append(
            f"{len(missing)} listed index(es) unreadable, their files count as unindexed: "
            + "; ".join(missing[:3])
            + (" ..." if len(missing) > 3 else "")
        )
    return seen, attempted, notes


register(FolderInventory())
