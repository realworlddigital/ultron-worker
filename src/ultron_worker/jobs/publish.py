"""catalog_publish: publish the NAS-side catalog into the backbone as ``record.media_asset``.

Model-free. The inputs are media folders (absolute or ``ROOT/relative``); the root's TOC
(``<root>/_index/catalog.jsonl``) names the folder indexes (``tags.jsonl``) under each input,
and every tagged record in them is one work item, so the runner checkpoints per entry and a
parked job resumes where it stopped. Per entry:

* identity: the record's ``content_hash`` (``sha256:<hex>``); a record without one is hashed
  once here and the hash is written back into its folder index (with any probed technical
  metadata), so no later job hashes it again;
* technical metadata from the record, else from ``ffprobe`` on the local file;
* one upsert through ``PUT /media/assets``: read ``records/mas_<sha256(content_hash)[:24]>``
  first, create when absent, otherwise merge (tags/technical/locations only ever gain; rights
  only move off ``needs_check``; org and provenance stay as first published) and write only
  when the merge changed something, checking the written version is the read one + 1.

Metadata only: media bytes are never moved and nothing local or in the backbone is deleted.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..backbone import BackboneError, BackboneUnavailable
from ..catalog import (
    _CATALOG_LOCK,
    CATALOG_DIR,
    TOC_NAME,
    _write,
    file_stamp,
    has_stamp,
    stamp_matches,
)
from ..inbox import sha256
from ..media import MediaRoots, _parts, _tagged, is_absolute, read_index
from . import register
from .base import (
    KEY_MAX,
    ItemFailed,
    JobContext,
    JobError,
    JobKind,
    JobSpec,
    JobStopped,
    Output,
    WorkItem,
    atomic_write_text,
)
from .vision import FRAME_TIMEOUT, IMAGE_EXTS, VIDEO_EXTS

STORAGE_ID = "nas-main"
FEEDER = "nas_mirror"
INGESTED_BY = "ultron"
UNASSIGNED = "unassigned"
# record.media_asset's org enum. It has no "unassigned" yet, so an entry without an org from
# the job's mapping is held (hashed, not published) rather than given a guessed org.
BACKBONE_ORGS = frozenset({"guild_hall", "gcs", "gea", "personal"})
RIGHTS = ("cleared", "needs_check", "blocked")
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
CONTENT_HASH = re.compile(r"sha256:[0-9a-f]{64}")
HEX = re.compile(r"[0-9a-fA-F]{64}")
FACET = re.compile(r"[a-z][a-z0-9_]{0,39}")
TECHNICAL = ("duration_sec", "width", "height", "codec", "fps", "size_bytes")
WRITEBACK_EVERY = 25  # pending hashes per index before they are flushed to it
REVERIFY_AFTER = timedelta(days=7)  # a location verified more recently is left as it is
MAX_FLAT, MAX_FACET_ITEMS, MAX_LOCATIONS = 200, 50, 20
NAMED_MAX = 20  # failures/holds named in the note; the report file has them all
PLAN_CACHE, PENDING_CACHE = "publish.plan", "publish.pending"


def asset_id(content_hash: str) -> str:
    """The backbone's server-derived record id for an asset (media_catalog.asset_record_id)."""
    import hashlib

    return "mas_" + hashlib.sha256(content_hash.encode()).hexdigest()[:24]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class CatalogPublish(JobKind):
    name = "catalog_publish"
    needs_model = False

    # ---- spec --------------------------------------------------------------------
    def validate(self, spec: JobSpec, ctx: JobContext) -> None:
        super().validate(spec, ctx)
        out = ctx.allowed(spec.output, what="output target")
        if out.suffix.lower() != ".json" or out.is_dir():
            raise JobError(f"output target {str(out)!r} must be a .json report file")
        if out.name in (TOC_NAME, "tags.jsonl") or out.parent.name == CATALOG_DIR:
            raise JobError(f"output target {str(out)!r} would overwrite a catalog file")
        for raw in spec.inputs:
            p = ctx.allowed(raw, what="input")
            if not p.is_dir():
                raise JobError(f"input {raw!r} is not a folder")
            if ctx.media_roots.relativize(p) is None:
                raise JobError(f"input {raw!r} is not under a named media root")
        _orgs(spec, ctx)
        if getattr(ctx, "backbone", None) is None:
            raise JobError("catalog_publish needs the runner's backbone client")

    def plan(self, spec: JobSpec, ctx: JobContext) -> list[WorkItem]:
        roots: MediaRoots = ctx.media_roots
        orgs = _orgs(spec, ctx)
        items: list[WorkItem] = []
        notes: list[str] = []
        seen: set[tuple[str, str]] = set()
        for raw in spec.inputs:
            root, folder = roots.relativize(ctx.allowed(raw, what="input"))  # type: ignore[misc]
            label = f"{root}/{folder}" if folder else root
            indexes = _indexes(root, folder, ctx)
            if not indexes:
                raise JobError(
                    f"{label}: no folder index (tags.jsonl) in the TOC or in the folder; tag it "
                    "before publishing"
                )
            records: list[tuple[Path, dict[str, Any]]] = []
            for index in indexes:
                try:
                    recs = read_index(index)
                except (OSError, ValueError) as exc:
                    raise JobError(f"index {str(index)!r} can't be read: {exc}") from exc
                records.extend((index, roots.normalize(r)) for r in recs)
            untagged = 0
            mine: list[WorkItem] = []
            for index, rec in records:
                if "root" not in rec or roots.lookup(str(rec["root"])) != roots.lookup(root):
                    continue
                if not _under(rec["relpath"], folder, roots, root):
                    continue
                key = roots.identity(rec)
                if key in seen:
                    continue
                seen.add(key)
                if not _tagged(rec):
                    untagged += 1
                    continue
                ref = f"{rec['root']}/{rec['relpath']}"
                mine.append(
                    WorkItem(
                        key=ref[:KEY_MAX],
                        label=ref,
                        payload={
                            "path": ref,
                            "folder": label,
                            "index": str(index),
                            "record": rec,
                            "org": _org_for(rec, orgs, roots),
                        },
                    )
                )
            if mine:
                mine[-1].payload["last"] = True
            items.extend(mine)
            notes.append(
                f"{label}: {len(mine)} tagged entr{'y' if len(mine) == 1 else 'ies'} from "
                f"{len(indexes)} index(es)"
                + (f"; {untagged} untagged not published" if untagged else "")
            )
        ctx.cache[PLAN_CACHE] = {"notes": notes}
        return items

    # ---- one entry ---------------------------------------------------------------
    def run_item(self, item: WorkItem, spec: JobSpec, ctx: JobContext) -> dict[str, Any]:
        rec: dict[str, Any] = item.payload["record"]
        index = Path(item.payload["index"])
        where = ctx.media_roots.absolute(rec)
        if where is None:
            raise ItemFailed("no path under the configured media roots")
        path = ctx.allowed(where, what="media file")
        if not path.is_file():
            raise ItemFailed("file missing on disk")
        stamp = file_stamp(path)
        if has_stamp(rec) and not stamp_matches(rec, stamp):
            raise ItemFailed("file changed since it was tagged; re-tag before publishing")
        result: dict[str, Any] = {"path": item.label, "folder": item.payload["folder"]}
        writeback: dict[str, Any] = {}
        content_hash = _content_hash(rec)
        if content_hash is None:
            with ctx.timing.phase("hash"):
                digest = sha256(path, ctx.stop)
            if digest is None:
                raise JobStopped
            content_hash = writeback["content_hash"] = f"sha256:{digest}"
            result["hashed"] = True
        technical = _technical(rec)
        if technical is None:
            with ctx.timing.phase("ffprobe"):
                technical, probed = _probe(path)
            if probed:
                writeback["technical"] = dict(technical)
            else:
                result["unprobed"] = True
        technical.setdefault("size_bytes", stamp["size_bytes"])
        result["content_hash"] = content_hash
        if writeback:
            result["writeback"] = writeback
            _queue(ctx, index, ctx.media_roots.identity(rec), writeback)
        org = item.payload["org"]
        try:
            if org not in BACKBONE_ORGS:
                result["held"] = f"org {org!r} is not a backbone org; supply params.orgs"
                return result
            body = _asset(rec, path, content_hash, technical, org, ctx)
            result.update(_upsert(body, ctx))
            return result
        finally:
            if item.payload.get("last") or _pending_count(ctx, index) >= WRITEBACK_EVERY:
                _flush(ctx, index)

    # ---- result ------------------------------------------------------------------
    def finalize(
        self, spec: JobSpec, results: list[tuple[WorkItem, dict[str, Any]]], ctx: JobContext
    ) -> Output:
        for item, res in results:  # hashes from checkpoints of an earlier, interrupted attempt
            if res.get("writeback"):
                rec = item.payload["record"]
                _queue(
                    ctx,
                    Path(item.payload["index"]),
                    ctx.media_roots.identity(rec),
                    res["writeback"],
                )
        for index in list(ctx.cache.get(PENDING_CACHE, {})):
            _flush(ctx, Path(index))
        out = ctx.allowed(spec.output, what="output target")
        folders: dict[str, dict[str, int]] = {}
        hashes: set[str] = set()
        named: dict[str, list[str]] = {"failed": [], "held": [], "conflict": [], "org": []}
        rows = []
        for item, res in results:
            counts = folders.setdefault(
                item.payload["folder"],
                dict.fromkeys(
                    ("created", "updated", "skipped", "duplicates", "held", "failed", "hashed"), 0
                ),
            )
            if "skipped" in res or "failed" in res:
                outcome = "failed"
                named["failed"].append(f"{item.label} ({res.get('skipped') or res.get('failed')})")
            elif "held" in res:
                outcome = "held"
                named["held"].append(item.label)
            elif res.get("content_hash") in hashes:
                outcome = "duplicates"
            else:
                outcome = res["outcome"]
                hashes.add(res["content_hash"])
            counts[outcome] += 1
            counts["hashed"] += bool(res.get("hashed"))
            if res.get("conflict"):
                named["conflict"].append(item.label)
            if res.get("org_kept"):
                named["org"].append(f"{item.label} (kept {res['org_kept']})")
            extra = {k: v for k, v in res.items() if k not in ("path", "folder", "writeback")}
            rows.append({"path": item.label, "folder": item.payload["folder"], **extra})
            rows[-1]["outcome"] = outcome
        total = {k: sum(c[k] for c in folders.values()) for k in next(iter(folders.values()), {})}
        report = {
            "kind": self.name,
            "task_id": ctx.task_id,
            "storage_id": STORAGE_ID,
            "published_at": _now(),
            "distinct_assets": len(hashes),
            "totals": total,
            "folders": folders,
            "items": rows,
        }
        atomic_write_text(out, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        lines = [
            f"Published {len(results)} catalog entr{'y' if len(results) == 1 else 'ies'} from "
            f"{len(folders)} folder(s) as record.media_asset: {_counts(total)}; "
            f"{len(hashes)} distinct asset(s) in the backbone"
        ]
        lines += [f"- {folder}: {_counts(c)}" for folder, c in folders.items()]
        lines += [f"Plan: {n}" for n in ctx.cache.get(PLAN_CACHE, {}).get("notes", [])]
        for key, what in (
            ("failed", "Not published (failed)"),
            ("held", f"Held, no backbone org (no org mapping; {UNASSIGNED!r} isn't one)"),
            ("conflict", "Written while another writer changed the record (version jumped)"),
            ("org", "Already published under another org (kept)"),
        ):
            if named[key]:
                more = len(named[key]) - NAMED_MAX
                lines.append(
                    f"{what}: {len(named[key])}: "
                    + "; ".join(named[key][:NAMED_MAX])
                    + (f"; ... {more} more in the report" if more > 0 else "")
                )
        return Output(paths=[str(out)], summary="\n".join(lines))


# ---- inputs ------------------------------------------------------------------------
def _orgs(spec: JobSpec, ctx: JobContext) -> list[tuple[tuple[str, str], str]]:
    """``params.org`` (every input) and ``params.orgs`` ({folder ref: org}, the deepest
    matching folder wins) as [((root, folder), org)], deepest first. Never derived from names."""
    roots: MediaRoots = ctx.media_roots
    default = spec.params.get("org", UNASSIGNED)
    mapping = spec.params.get("orgs") or {}
    if not isinstance(mapping, dict):
        raise JobError("params.orgs must map folder refs to orgs")
    out: list[tuple[tuple[str, str], str]] = []
    for ref, org in [*mapping.items()]:
        _check_org(org, f"params.orgs[{ref!r}]")
        found = roots.relativize(ctx.allowed(ref, what="params.orgs folder"))
        if found is None:
            raise JobError(f"params.orgs folder {ref!r} is not under a named media root")
        out.append((found, org))
    _check_org(default, "params.org")
    out.sort(key=lambda e: -len(_parts(e[0][1])))
    out.append((("", ""), default))
    return out


def _check_org(org: Any, what: str) -> None:
    if org != UNASSIGNED and org not in BACKBONE_ORGS:
        raise JobError(f"{what}={org!r}: one of {sorted(BACKBONE_ORGS)} or {UNASSIGNED!r}")


def _org_for(
    rec: dict[str, Any], orgs: list[tuple[tuple[str, str], str]], roots: MediaRoots
) -> str:
    for (root, folder), org in orgs:
        if not root or (
            roots.lookup(root) == roots.lookup(rec["root"])
            and _under(rec["relpath"], folder, roots, root)
        ):
            return org
    return UNASSIGNED


def _under(relpath: str, folder: str, roots: MediaRoots, root: str) -> bool:
    if not folder:
        return True
    name = roots.lookup(root)
    fold = roots.folds[name] if name else False
    rel, base = (relpath.casefold(), folder.casefold()) if fold else (relpath, folder)
    return rel.startswith(base.rstrip("/") + "/")


def _indexes(root: str, folder: str, ctx: JobContext) -> list[Path]:
    """The folder indexes the TOC lists at or under ``folder``; else ``<folder>/tags.jsonl``."""
    roots: MediaRoots = ctx.media_roots
    found: list[Path] = []
    where = roots.resolve(root, f"{CATALOG_DIR}/{TOC_NAME}")
    toc = ctx.allowed(where, what="catalog") if where else None
    if toc is not None and toc.is_file():
        try:
            entries = read_index(toc)
        except (OSError, ValueError) as exc:
            raise JobError(f"catalog {str(toc)!r} can't be read: {exc}") from exc
        for entry in entries:
            if roots.lookup(str(entry.get("root") or root)) != roots.lookup(root):
                continue
            name, ref = str(entry.get("folder") or ""), entry.get("index")
            inside = _under(f"{name}/", folder, roots, root) or not folder
            if not inside or not isinstance(ref, str) or not ref:
                continue
            target = ref if is_absolute(ref) else roots.resolve(root, ref)
            path = ctx.allowed(target, what="folder index")
            if path.is_file() and path not in found:
                found.append(path)
    if not found:
        local = roots.resolve(root, f"{folder}/tags.jsonl" if folder else "tags.jsonl")
        path = ctx.allowed(local, what="folder index") if local else None
        if path is not None and path.is_file():
            found.append(path)
    return found


# ---- mapping -----------------------------------------------------------------------
def _content_hash(rec: dict[str, Any]) -> str | None:
    for key in ("content_hash", "sha256", "hash"):
        value = rec.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        if CONTENT_HASH.fullmatch(value.lower()):
            return value.lower()
        if HEX.fullmatch(value):
            return f"sha256:{value.lower()}"
    return None


def _number(value: Any, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return int(value) if kind is int else float(value)


def _clean_technical(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("duration_sec", "fps"):
        if (v := _number(raw.get(key), float)) is not None:
            out[key] = round(v, 3)
    for key in ("width", "height", "size_bytes"):
        if (v := _number(raw.get(key), int)) is not None:
            out[key] = v
    if isinstance(raw.get("codec"), str) and raw["codec"]:
        out["codec"] = raw["codec"][:60]
    return out


def _technical(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The record's technical metadata, or None when it has none worth keeping (then probe)."""
    raw = rec.get("technical") if isinstance(rec.get("technical"), dict) else rec
    tech = _clean_technical(raw)
    if not any(k in tech for k in ("duration_sec", "width", "height", "codec")):
        return None
    return tech


def _rate(value: Any) -> float | None:
    try:
        num, _, den = str(value).partition("/")
        rate = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _probe(path: Path) -> tuple[dict[str, Any], bool]:
    """(technical, probed) from ffprobe on the local file; ({}, False) when it can't."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {}, False
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=FRAME_TIMEOUT, check=True,
        )  # fmt: skip
        doc = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}, False
    streams = [s for s in doc.get("streams") or [] if isinstance(s, dict)]
    stream = next((s for s in streams if s.get("codec_type") == "video"), None) or next(
        iter(streams), {}
    )
    try:
        duration = float((doc.get("format") or {}).get("duration") or 0) or None
    except (TypeError, ValueError):
        duration = None
    fps = _rate(stream.get("avg_frame_rate")) if stream.get("codec_type") == "video" else None
    raw = {
        "duration_sec": duration,
        "width": stream.get("width"),
        "height": stream.get("height"),
        "codec": stream.get("codec_name"),
        "fps": fps if duration else None,
    }
    tech = _clean_technical(raw)
    return tech, bool(tech)


def _kind(rec: dict[str, Any], path: Path) -> str:
    media = rec.get("media")
    if media in ("video", "image", "audio"):
        return str(media)
    suffix = path.suffix.lower()
    if suffix in VIDEO_EXTS:
        return "video"
    if suffix in AUDIO_EXTS:
        return "audio"
    if suffix in IMAGE_EXTS:
        return "image"
    raise ItemFailed(f"not a video, image or audio file ({suffix or 'no extension'})")


def _strings(value: Any, limit: int) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else value if isinstance(value, list) else []
    out: list[str] = []
    for v in raw:
        s = " ".join(str(v).split())[:limit] if isinstance(v, str | int | float) else ""
        if s and s not in out:
            out.append(s)
    return out


def _tags(rec: dict[str, Any]) -> dict[str, Any]:
    """structured: caption, keywords (the tagger's tags), folder, plus any facet dict the record
    carries (``structured``/``facets``); flat: the lowercased keywords and facet values."""
    structured: dict[str, Any] = {}
    facets = rec.get("structured") if isinstance(rec.get("structured"), dict) else rec.get("facets")
    for key, value in (facets or {}).items() if isinstance(facets, dict) else []:
        if not isinstance(key, str) or not FACET.fullmatch(key):
            continue
        if isinstance(value, str) and value.strip():
            structured[key] = " ".join(value.split())[:200]
        elif vals := _strings(value, 200)[:MAX_FACET_ITEMS]:
            structured[key] = vals
    if isinstance(rec.get("caption"), str) and rec["caption"].strip():
        structured["caption"] = " ".join(rec["caption"].split())[:200]
    if keywords := _strings(rec.get("tags"), 200)[:MAX_FACET_ITEMS]:
        structured["keywords"] = keywords
    folder = "/".join(_parts(str(rec.get("relpath") or ""))[:-1])
    if folder:
        structured.setdefault("folder", folder[:200])
    flat: list[str] = []
    for key, value in structured.items():
        if key in ("caption", "folder"):
            continue
        for v in [value] if isinstance(value, str) else value:
            s = v.lower()[:80]
            if s not in flat:
                flat.append(s)
    return {"structured": dict(list(structured.items())[:50]), "flat": flat[:MAX_FLAT]}


def _rights(rec: dict[str, Any]) -> str:
    for key in ("clearance", "rights"):
        value = rec.get(key)
        if isinstance(value, dict):
            value = value.get("status") or value.get("rights")
        if isinstance(value, str) and value.strip().lower() in RIGHTS:
            return value.strip().lower()
    return "needs_check"


def _location(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "storage_id": STORAGE_ID,
        "path": f"{rec['root']}/{rec['relpath']}",
        "status": "verified",
        "last_verified_at": _now(),
    }


def _asset(
    rec: dict[str, Any], path: Path, content_hash: str, technical: dict[str, Any], org: str,
    ctx: JobContext,
) -> dict[str, Any]:  # fmt: skip
    return {
        "content_hash": content_hash,
        "kind": _kind(rec, path),
        "tags": _tags(rec),
        "technical": technical,
        "provenance": {
            "feeder": FEEDER,
            "ingested_by": INGESTED_BY,
            "ingested_at": _now(),
            "source_ref": ctx.task_id,
        },
        "rights": _rights(rec),
        "org": org,
        "locations": [_location(rec)],
    }


def _union(old: list[str], new: list[str], limit: int) -> list[str]:
    out = list(old)
    out += [v for v in new if v not in out]
    return out[: max(limit, len(old))]


def _merge(cur: dict[str, Any], ours: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The published record after adding this entry, plus notes. Only ever adds: tags and
    technical fill in, a location is added or re-verified, rights move off needs_check; kind,
    org and provenance stay as first published. Replaying the same entry changes nothing."""
    out = json.loads(json.dumps(cur))
    notes: dict[str, Any] = {}
    tags = out.setdefault("tags", {"structured": {}, "flat": []})
    structured = tags.setdefault("structured", {})
    for key, value in ours["tags"]["structured"].items():
        if key not in structured and len(structured) < 50:
            structured[key] = value
        elif isinstance(value, list) and isinstance(structured.get(key), list):
            structured[key] = _union(structured[key], value, MAX_FACET_ITEMS)
    tags["flat"] = _union(tags.setdefault("flat", []), ours["tags"]["flat"], MAX_FLAT)
    technical = out.setdefault("technical", {})
    for key, value in ours["technical"].items():
        if technical.get(key) is None:
            technical[key] = value
    if out.get("rights") == "needs_check" and ours["rights"] != "needs_check":
        out["rights"] = ours["rights"]
    if out.get("org") != ours["org"]:
        notes["org_kept"] = out.get("org")
    mine = ours["locations"][0]
    locations = out.setdefault("locations", [])
    for i, loc in enumerate(locations):
        if (loc.get("storage_id"), loc.get("path")) == (mine["storage_id"], mine["path"]):
            if loc.get("status") != "verified" or _stale(loc.get("last_verified_at")):
                locations[i] = mine
            break
    else:
        if len(locations) < MAX_LOCATIONS:
            locations.append(mine)
        else:
            notes["locations_full"] = True
    return out, notes


def _stale(stamp: Any) -> bool:
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return datetime.now(UTC) - when > REVERIFY_AFTER


def _upsert(body: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Create, update or skip one asset. A backbone outage parks the job (retryable; every
    entry before this one is checkpointed); a refused key stops it; any other refusal fails
    just this entry."""
    bb = ctx.backbone
    aid = asset_id(body["content_hash"])
    try:
        with ctx.timing.phase("backbone"):
            cur = bb.media_asset(aid)
            if cur is None:
                bb.put_media_asset(body)
                return {"outcome": "created", "asset_id": aid}
            data, version = cur.get("data") or {}, cur.get("version")
            if data.get("content_hash") != body["content_hash"]:
                raise ItemFailed(f"records/{aid} holds another content_hash")
            merged, notes = _merge(data, body)
            if merged == data:
                return {"outcome": "skipped", "asset_id": aid, **notes}
            res = bb.put_media_asset(merged)
    except BackboneUnavailable as exc:
        for index in list(ctx.cache.get(PENDING_CACHE, {})):
            _flush(ctx, Path(index))
        raise JobError(
            f"backbone unreachable mid-publish ({exc}); progress is checkpointed and the job "
            "resumes from this entry",
            retryable=True,
        ) from exc
    except BackboneError as exc:
        if exc.status in (401, 403):
            raise JobError(f"backbone refused the runner's key for media assets: {exc}") from exc
        raise ItemFailed(str(exc)) from exc
    written = ((res or {}).get("doc") or {}).get("version")
    result = {"outcome": "updated", "asset_id": aid, **notes}
    if isinstance(version, int) and isinstance(written, int) and written != version + 1:
        result["conflict"] = True
    return result


# ---- local write-back ----------------------------------------------------------------
def _queue(ctx: JobContext, index: Path, key: tuple[str, str], fields: dict[str, Any]) -> None:
    pending = ctx.cache.setdefault(PENDING_CACHE, {}).setdefault(str(index), {})
    pending.setdefault(key, {}).update(fields)


def _pending_count(ctx: JobContext, index: Path) -> int:
    return len(ctx.cache.get(PENDING_CACHE, {}).get(str(index), {}))


def _flush(ctx: JobContext, index: Path) -> None:
    """Write queued hashes (and probed technical metadata) into their records of ``index``.
    A record that already has a field keeps it; nothing else in the index changes."""
    pending = ctx.cache.get(PENDING_CACHE, {}).pop(str(index), None)
    if not pending:
        return
    roots: MediaRoots = ctx.media_roots
    with _CATALOG_LOCK:
        text = index.read_text(encoding="utf-8")
        jsonl = index.suffix.lower() == ".jsonl"
        doc: Any = None
        if jsonl:
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            doc = json.loads(text)
            records = doc.get("items", []) if isinstance(doc, dict) else doc
        changed = False
        for rec in records:
            if not isinstance(rec, dict):
                continue
            fields = pending.get(roots.identity(rec))
            for key, value in (fields or {}).items():
                missing = _content_hash(rec) if key == "content_hash" else _technical(rec)
                if missing is None:
                    rec[key], changed = value, True
        if not changed:
            return
        if jsonl:
            _write(index, records)
        else:
            atomic_write_text(index, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")


def _counts(c: dict[str, int]) -> str:
    return (
        f"{c.get('created', 0)} created, {c.get('updated', 0)} updated, "
        f"{c.get('skipped', 0)} skipped, {c.get('duplicates', 0)} duplicates collapsed, "
        f"{c.get('held', 0)} held, {c.get('failed', 0)} failed, {c.get('hashed', 0)} hashed"
    )


register(CatalogPublish())
