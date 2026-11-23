"""Media roots: tag records are keyed by a root *name* plus a path relative to it, never by an
absolute path, so the library index survives a base-path change (a NAS swap, a new drive letter).

``media_roots`` maps a name to a base path (``[media_roots]`` in config.toml). Without that
table each ``runner.allowed_roots`` entry becomes a root named after its final component, so
``\\\\GUILD-NAS\\GUILD-RAW-Media`` is the root ``GUILD-RAW-Media``. A record stores ``root`` and
``relpath`` (forward slashes); its absolute path is derived from the mapping when it is read.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .sharedfile import open_shared

_DRIVE = re.compile(r"^[A-Za-z]:([\\/]|$)")


def _expand(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def _windowsy(path: str) -> bool:
    return "\\" in path or bool(_DRIVE.match(path))


def _folds(path: str) -> bool:
    """Whether paths under ``path`` compare case-insensitively (Windows, drive or UNC paths)."""
    return os.name == "nt" or _windowsy(path)


def is_absolute(path: str) -> bool:
    return path.startswith(("/", "\\")) or bool(_DRIVE.match(path)) or os.path.isabs(path)


def _parts(path: str) -> list[str]:
    return [p for p in re.split(r"[\\/]+", path) if p]


def _norm(path: str) -> str:
    """Separator-agnostic form of an absolute path: ``/`` separators, no trailing slash, the
    leading ``//`` of a UNC path kept."""
    s = path.replace("\\", "/")
    lead = "//" if s.startswith("//") else ("/" if s.startswith("/") else "")
    return lead + "/".join(_parts(s))


def root_name(base: str) -> str | None:
    """The derived name of a root: its final path component (``D:\\Photos`` -> ``Photos``)."""
    parts = _parts(base)
    if not parts or parts[-1].endswith(":"):  # a bare drive (D:\) has no usable name
        return None
    return parts[-1]


def valid_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and not set(name) & set("\\/:")


class MediaRoots:
    """name -> base path, with the conversions between absolute paths and root + relpath."""

    def __init__(self, roots: Mapping[str, str]) -> None:
        self.roots: dict[str, str] = {}
        seen: dict[str, str] = {}
        for name, base in roots.items():
            if not valid_name(name):
                raise ValueError(f"media root name {name!r} must be a plain folder-style name")
            if name.casefold() in seen:
                raise ValueError(
                    f"media root names {seen[name.casefold()]!r} and {name!r} differ only in case"
                )
            seen[name.casefold()] = name
            self.roots[name] = _expand(str(base))
        # Case-insensitive per root: Windows/UNC bases fold, POSIX bases (on POSIX) don't.
        self.folds = {n: _folds(b) for n, b in self.roots.items()}

    @classmethod
    def build(
        cls, explicit: Mapping[str, str] | None, allowed_roots: Iterable[str | os.PathLike[str]]
    ) -> MediaRoots:
        """The explicit table wins; allowed roots it doesn't cover are added under their derived
        names (a derived name two allowed roots share is ambiguous and left out)."""
        roots = dict(explicit or {})
        covered = {cls._key_for(str(b)) for b in roots.values()}
        taken = {n.casefold() for n in roots}
        derived: dict[str, list[tuple[str, str]]] = {}  # names that differ only in case collide
        for base in allowed_roots:
            b = str(base)
            name = root_name(b)
            if name and cls._key_for(b) not in covered:
                derived.setdefault(name.casefold(), []).append((name, b))
        for folded, found in derived.items():
            if len(found) == 1 and folded not in taken and valid_name(found[0][0]):
                roots[found[0][0]] = found[0][1]
        return cls(roots)

    @staticmethod
    def _key_for(path: str) -> str:
        n = _norm(_expand(path))
        return n.casefold() if _folds(path) else n

    @staticmethod
    def _cmp(path: str, fold: bool) -> str:
        n = _norm(path)
        return n.casefold() if fold else n

    def _bases(self, name: str) -> list[str]:
        base = self.roots[name]
        out = [base]
        if _windowsy(base) and os.name != "nt":
            return out
        try:  # a mapped drive resolves to its UNC path on Windows: match either
            resolved = str(Path(base).resolve(strict=False))
        except OSError:
            resolved = base
        if resolved != base:
            out.append(resolved)
        return out

    def lookup(self, name: str) -> str | None:
        if name in self.roots:
            return name
        for n in self.roots:  # names are unique ignoring case
            if n.casefold() == name.casefold():
                return n
        return None

    def relativize(self, path: str | os.PathLike[str]) -> tuple[str, str] | None:
        """``(root, relpath)`` for an absolute path under a known root (longest match), else
        None. ``relpath`` keeps the path's own spelling, with forward slashes."""
        p = str(path)
        best: tuple[int, str, str] | None = None
        for name in self.roots:
            fold = self.folds[name]
            target = self._cmp(p, fold)
            for base in self._bases(name):
                b = self._cmp(base, fold)
                under = target == b or target.startswith(b.rstrip("/") + "/")
                if under and (best is None or len(b) > best[0]):
                    rel = "/".join(_parts(_norm(p))[len(_parts(b)) :])
                    best = (len(b), name, rel)
        return (best[1], best[2]) if best else None

    def resolve(self, root: str, relpath: str) -> str | None:
        """The absolute path of ``root`` + ``relpath`` under the current mapping."""
        name = self.lookup(root)
        if name is None:
            return None
        base = self.roots[name]
        rel = _parts(relpath)
        if not rel:
            return base
        sep = "\\" if _windowsy(base) and "/" not in base else "/"
        return base.rstrip("\\/") + sep + sep.join(rel)

    def resolve_ref(self, ref: str | os.PathLike[str]) -> str:
        """A job path: absolute paths pass through; ``ROOT/relative/...`` resolves through the
        mapping; anything else is returned unchanged."""
        s = _expand(str(ref))
        if is_absolute(s):
            return s
        parts = _parts(s)
        if parts and (name := self.lookup(parts[0])) is not None:
            return self.resolve(name, "/".join(parts[1:])) or s
        return s

    # ---- records -----------------------------------------------------------------
    def normalize(self, record: dict[str, Any]) -> dict[str, Any]:
        """A record with ``root`` + ``relpath`` first. One that carries only an absolute
        ``path`` (an index from an older runner) gets them when the path is under a known
        root; otherwise it is returned as it is."""
        root, rel = record.get("root"), record.get("relpath")
        if isinstance(root, str) and root and isinstance(rel, str):
            out = {"root": root, "relpath": "/".join(_parts(rel))}
        else:
            path = record.get("path")
            found = self.relativize(path) if isinstance(path, str) and path else None
            if found is None:
                return dict(record)
            out = {"root": found[0], "relpath": found[1]}
        out.update((k, v) for k, v in record.items() if k not in out)
        return out

    def identity(self, record: dict[str, Any]) -> tuple[str, str]:
        """The merge/dedupe key: root + relpath (case-folded where paths are case-insensitive);
        a record outside every root falls back to its absolute path."""
        rec = self.normalize(record)
        if "root" in rec:
            name = self.lookup(str(rec["root"]))
            fold = self.folds[name] if name else os.name == "nt"
            root, rel = (name or str(rec["root"])), str(rec["relpath"])
            if name is None and fold:
                root = root.casefold()  # unconfigured root: no canonical spelling to use
            return (root, rel.casefold() if fold else rel)
        path = str(rec.get("path") or "")
        return ("", self._cmp(path, _folds(path)))

    def absolute(self, record: dict[str, Any]) -> str | None:
        """Where the record's file is now: derived from root + relpath, else its stored path."""
        rec = self.normalize(record)
        if "root" in rec:
            found = self.resolve(rec["root"], rec["relpath"])
            if found:
                return found
        path = rec.get("path")
        return str(path) if path else None

    def merge(self, *passes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge index passes in order, one record per identity. A later pass wins, except that
        a failed/skipped record never replaces a tagged one. First-seen order is kept."""
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for records in passes:
            for record in records:
                rec = self.normalize(record)
                key = self.identity(rec)
                old = merged.get(key)
                if old is not None and _tagged(old) and not _tagged(rec):
                    continue
                merged[key] = rec
        return list(merged.values())


def _tagged(record: dict[str, Any]) -> bool:
    return "failed" not in record and "skipped" not in record and bool(record.get("tags"))


def read_index(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """The records of a ``.jsonl`` (one per line) or ``.json`` (``{"items": [...]}`` or a list)
    index file, as stored. Raises ValueError on a file that isn't an index."""
    p = Path(path)
    with open_shared(p, "r", encoding="utf-8") as fh:  # never blocks a writer's replace
        text = fh.read()
    if p.suffix.lower() == ".jsonl":
        records = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"{p.name} line {n} is not JSON: {exc}") from exc
            if not isinstance(rec, dict):
                raise ValueError(f"{p.name} line {n} is not a record object")
            records.append(rec)
        return records
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{p.name} is not JSON: {exc}") from exc
    items = doc.get("items") if isinstance(doc, dict) else doc
    if not isinstance(items, list) or not all(isinstance(r, dict) for r in items):
        raise ValueError(f"{p.name} has no list of record objects")
    return items


def load_index(path: str | os.PathLike[str], roots: MediaRoots) -> list[dict[str, Any]]:
    """An index file's records, each normalized to root + relpath where its path allows."""
    return [roots.normalize(r) for r in read_index(path)]
