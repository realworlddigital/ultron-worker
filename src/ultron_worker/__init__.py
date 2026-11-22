"""Ultron local job runner (tsk_da8f7875f31e4d8298f4).

Backbone cards of type ``task.local_job`` with ``data.executor == "local-ultron"`` run on
local models on David's Surface ("Ultron") instead of Devin ACU or API tokens. The runner
only ever calls out (HTTPS polling of the backbone, localhost Ollama); its status GUI binds
to localhost and is a view, never a control plane.
"""

from __future__ import annotations

import os
import tomllib
from importlib import metadata
from pathlib import Path

DIST = "ultron-worker"
COMMIT_ENV = "ULTRON_COMMIT"  # the installed commit, set by the desktop app for its runner


PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"  # present in a clone


def _version(pyproject: Path = PYPROJECT) -> str:
    """pyproject.toml is the one source of truth: read it from the source tree when running from
    a clone (editable/dev), else from the installed package metadata pip built from it."""
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        if project.get("name") == DIST:
            return str(project["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version(DIST)
    except metadata.PackageNotFoundError:
        return "0+unknown"


def commit() -> str | None:
    """The installed commit (short sha) when the desktop app told us, else None."""
    sha = os.environ.get(COMMIT_ENV, "").strip()
    return sha[:7] or None


__version__ = _version()
