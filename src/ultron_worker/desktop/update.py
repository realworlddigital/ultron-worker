"""Check for updates against origin/main, and build a verified new install for David's one click.

Nothing here runs on its own: the app calls :func:`check` on launch and on the button, and
:func:`prepare` only after David presses Update. ``prepare`` builds the new version in a fresh
venv beside the running one and runs its ``--check``; the running runner is untouched until
that passes, so a failed update leaves the previous working install running.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import winenv
from .layout import NO_WINDOW, Install, now_iso, venv_python

REMOTE, BRANCH = "origin", "main"
# States in which the Update button is offered.
UPDATABLE = frozenset({"behind", "unknown"})


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str

    @property
    def short(self) -> str:
        return self.sha[:7]


def compare(ahead: int, behind: int) -> str:
    """Installed vs origin/main from ``rev-list --left-right --count installed...origin/main``."""
    if ahead < 0 or behind < 0:
        raise ValueError("commit counts can't be negative")
    if ahead == 0 and behind == 0:
        return "equal"
    if ahead == 0:
        return "behind"
    if behind == 0:
        return "ahead"
    return "diverged"


def parse_left_right(output: str) -> tuple[int, int]:
    """``"2\\t5"`` -> (2 ahead, 5 behind)."""
    parts = output.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise UpdateError(f"unexpected git rev-list output: {output!r}")
    return int(parts[0]), int(parts[1])


@dataclass
class UpdateStatus:
    state: str  # equal | behind | ahead | diverged | unknown | error
    current: Commit | None = None
    available: Commit | None = None
    ahead: int = 0
    behind: int = 0
    error: str | None = None
    checked_at: str = field(default_factory=now_iso)

    @property
    def can_update(self) -> bool:
        return self.state in UPDATABLE and self.available is not None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("current", "available"):
            c = getattr(self, key)
            d[key] = {"sha": c.sha, "short": c.short, "subject": c.subject} if c else None
        d["can_update"] = self.can_update
        return d


def git(repo: Path, *args: str, timeout: float = 120) -> str:
    exe = winenv.git_exe()
    if exe is None:
        raise UpdateError(winenv.GIT_NOT_FOUND)
    try:
        proc = subprocess.run(
            [exe, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=winenv.child_env(),  # git's helpers (ssh, credential manager) need the full PATH
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"git {args[0]} ({exe}): {exc}") from exc
    if proc.returncode != 0:
        raise UpdateError(f"git {' '.join(args)}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def _commit(repo: Path, rev: str) -> Commit:
    sha, _, subject = git(repo, "log", "-1", "--format=%H%x00%s", rev).partition("\0")
    return Commit(sha, subject)


def check(
    repo: Path | None,
    installed_sha: str | None,
    *,
    remote: str = REMOTE,
    branch: str = BRANCH,
    fetch: bool = True,
) -> UpdateStatus:
    """Compare the installed sha with ``remote/branch``; never raises."""
    if repo is None:
        return UpdateStatus("error", error="no repo recorded in install.json: re-run install.ps1")
    try:
        if fetch:
            git(repo, "fetch", "--quiet", remote, branch)
        available = _commit(repo, f"{remote}/{branch}")
        if not installed_sha:
            return UpdateStatus("unknown", available=available, error="installed version unknown")
        try:
            current = _commit(repo, installed_sha)
        except UpdateError:
            return UpdateStatus(
                "unknown", available=available, error=f"installed {installed_sha[:7]} not in repo"
            )
        ahead, behind = parse_left_right(
            git(repo, "rev-list", "--left-right", "--count", f"{current.sha}...{available.sha}")
        )
        return UpdateStatus(compare(ahead, behind), current, available, ahead, behind)
    except UpdateError as exc:
        return UpdateStatus("error", error=str(exc))


def pull(repo: Path, target_sha: str, *, branch: str = BRANCH) -> None:
    """Fast-forward the checkout to ``target_sha``; refuse a dirty tree or another branch."""
    head = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if head != branch:
        raise UpdateError(f"the checkout at {repo} is on {head!r}, not {branch!r}")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(f"the checkout at {repo} has local changes: commit or discard them")
    git(repo, "merge", "--ff-only", "--quiet", target_sha)
    head_sha = git(repo, "rev-parse", "HEAD")
    if head_sha != target_sha:  # local commits beyond the target: we'd install something else
        raise UpdateError(
            f"the checkout at {repo} is at {head_sha[:7]}, past {target_sha[:7]} "
            "(local commits): reset it to origin/main or re-run install.ps1"
        )


def pip(py: str, *args: str) -> list[str]:
    """``python -m pip`` that never touches a pip cache.

    The per-user cache (%LocalAppData%\\pip\\cache) can hold files this user can't read (mixed
    ownership/ACLs), and pip fails the install on them (Errno 13): an update must not depend on
    it. Run with :func:`pip_env` so pip's own children (build isolation) skip it too.
    """
    return [py, "-m", "pip", "--no-cache-dir", *args]


def pip_env() -> dict[str, str]:
    env = winenv.child_env()
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


def _run(
    cmd: list[str],
    log: Callable[[str], None],
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"{Path(cmd[0]).name}: {exc}") from exc
    for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines()[-15:]:
        log("  " + line)
    return proc


def prepare(
    install: Install,
    target: Commit,
    *,
    log: Callable[[str], None],
    base_python: str,
    check_timeout: float = 180,
) -> Path:
    """Pull, build ``venvs/<sha>`` (the install.ps1 pip steps) and pass its ``--check``.

    Returns the new venv. Raises :class:`UpdateError` with the running install untouched.
    """
    repo = install.repo
    if repo is None:
        raise UpdateError("no repo recorded in install.json: re-run install.ps1")
    log(f"pulling {target.short} into {repo}")
    pull(repo, target.sha)
    venv = install.root / "venvs" / target.sha[:12]
    if venv.exists():
        shutil.rmtree(venv)
    if _run([base_python, "-m", "venv", str(venv)], log, 300).returncode != 0:
        raise UpdateError("could not create the new venv")
    py = str(venv_python(venv))
    try:
        env = pip_env()
        _run(pip(py, "install", "--upgrade", "pip"), log, 600, env)
        spec = f"{repo}[{','.join(install.extras)}]" if install.extras else str(repo)
        if _run(pip(py, "install", spec), log, 1800, env).returncode != 0:
            raise UpdateError("pip install failed (see the update log)")
        log("running --check with the new version")
        proc = _run([*install.runner_command(venv), "--check"], log, check_timeout)
        if proc.returncode != 0:
            raise UpdateError("the new version's --check failed; staying on the current install")
    except BaseException:
        shutil.rmtree(venv, ignore_errors=True)
        raise
    return venv
