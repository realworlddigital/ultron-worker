"""The environment the app gives its children, and finding git.

An app started from the Start Menu, the Run key or a PyInstaller launcher can carry a PATH
older than the one an interactive PowerShell sees (installed-since software, e.g. Git), plus
the launcher's own ``_PYI_*`` variables. Children get the app's environment with PATH merged
with the registry's current Machine and User PATH, so they see what David's shell sees.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

WINDOWS = os.name == "nt"
_MACHINE_ENV = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_LAUNCHER_VARS = ("_MEIPASS", "_PYI_")  # PyInstaller bootloader state, meaningless to children


def registry_path() -> tuple[str, str]:
    """(Machine PATH, User PATH) as the registry has them now, expanded; ("", "") off Windows."""
    if not WINDOWS:
        return "", ""
    import winreg

    def read(hive: int, key: str) -> str:
        try:
            with winreg.OpenKey(hive, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
        except OSError:
            return ""
        return winreg.ExpandEnvironmentStrings(str(value))

    return read(winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENV), read(
        winreg.HKEY_CURRENT_USER, "Environment"
    )


def _norm(entry: str) -> str:
    return os.path.normcase(entry.strip().rstrip("\\/"))


def merge_path(*parts: str | Iterable[str]) -> str:
    """Join PATH strings/lists in order, dropping empties and repeats (case-insensitive on
    Windows), so the first occurrence of every directory wins."""
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        entries = part.split(os.pathsep) if isinstance(part, str) else part
        for entry in entries:
            key = _norm(entry)
            if key and key not in seen:
                seen.add(key)
                out.append(entry.strip())
    return os.pathsep.join(out)


def _path_key(env: Mapping[str, str]) -> str:
    return next((k for k in env if k.upper() == "PATH"), "PATH")


def child_env(
    base: Mapping[str, str] | None = None,
    *,
    prepend: Iterable[Path | str] = (),
    registry: Callable[[], tuple[str, str]] = registry_path,
) -> dict[str, str]:
    """The user's full environment for a child: PATH refreshed from the registry (current
    entries first), ``prepend`` dirs in front, launcher variables dropped, and unbuffered UTF-8
    Python output so the app sees a crashing child's last words."""
    env = {
        k: v
        for k, v in (os.environ if base is None else base).items()
        if not k.upper().startswith(_LAUNCHER_VARS)
    }
    key = _path_key(env)
    machine, user = registry()
    env[key] = merge_path([str(p) for p in prepend], env.get(key, ""), machine, user)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def git_candidates(env: Mapping[str, str] | None = None) -> list[Path]:
    """Where Git for Windows lands when it isn't on PATH (machine-wide, per user, scoop)."""
    env = os.environ if env is None else env
    get = {k.upper(): v for k, v in env.items()}.get
    bases = [
        get("PROGRAMFILES"),
        get("PROGRAMW6432"),
        get("PROGRAMFILES(ARM)"),
        get("PROGRAMFILES(X86)"),
        r"C:\Program Files",
    ]
    out: list[Path] = []
    for base in bases:
        if base:
            out += [Path(base) / "Git" / "cmd" / "git.exe", Path(base) / "Git" / "bin" / "git.exe"]
    if get("LOCALAPPDATA"):
        local = Path(get("LOCALAPPDATA")) / "Programs" / "Git"
        out += [local / "cmd" / "git.exe", local / "bin" / "git.exe"]
    if get("USERPROFILE"):
        out.append(Path(get("USERPROFILE")) / "scoop" / "shims" / "git.exe")
    return list(dict.fromkeys(out))


def find_git(
    env: Mapping[str, str] | None = None,
    *,
    registry: Callable[[], tuple[str, str]] = registry_path,
    candidates: Iterable[Path] | None = None,
    which: Callable[..., str | None] = shutil.which,
    is_file: Callable[[Path], bool] = Path.is_file,
) -> str | None:
    """git on this process's PATH, else on the registry's current PATH, else in the standard
    install locations; None if it is nowhere."""
    env = os.environ if env is None else env
    found = which("git", path=env.get(_path_key(env), ""))
    if found:
        return found
    machine, user = registry()
    if machine or user:
        found = which("git", path=merge_path(machine, user))
        if found:
            return found
    for path in git_candidates(env) if candidates is None else candidates:
        if is_file(path):
            return str(path)
    return None


_git: str | None = None


def git_exe() -> str | None:
    """:func:`find_git`, remembered while the found file still exists."""
    global _git
    if not (_git and Path(_git).is_file()):
        _git = find_git()
    return _git


GIT_NOT_FOUND = (
    "git not found (looked on PATH, the registry PATH and the Git for Windows install folders):"
    " install Git for Windows, or add git.exe to PATH"
)
