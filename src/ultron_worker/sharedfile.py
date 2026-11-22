"""Open files that several processes use at once (``ultron.log``) so nobody locks anyone out.

On Windows, Python's ``open()`` shares read and write but not delete, so while the app's log
pane (or a second runner) has the file open, a rename for rotation fails, and a handle opened
without write sharing blocks the writer's open outright. Here the file is opened with
FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE. Elsewhere this is plain ``open()``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any

WINDOWS = os.name == "nt"
FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_READ_ATTRIBUTES = 0x80
_OPEN_EXISTING = 3
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80


def win_flags(mode: str) -> tuple[int, int, int]:
    """(desired access, creation disposition, CRT flags) for ``mode`` ("r", "rb", "a", "ab")."""
    base = mode.replace("b", "").replace("t", "")
    if base == "r":
        return _GENERIC_READ, _OPEN_EXISTING, os.O_RDONLY
    if base == "a":
        return _GENERIC_WRITE | _FILE_READ_ATTRIBUTES, _OPEN_ALWAYS, os.O_WRONLY | os.O_APPEND
    raise ValueError(f"open_shared supports 'r' and 'a' modes, not {mode!r}")


def _win_open(path: Path, mode: str) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    access, disposition, crt = win_flags(mode)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = k32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    handle = create(
        str(path), access, FILE_SHARE_ALL, None, disposition, _FILE_ATTRIBUTE_NORMAL, None
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        raise OSError(None, ctypes.FormatError(err).strip(), str(path), err)
    try:
        return msvcrt.open_osfhandle(handle, crt | getattr(os, "O_BINARY", 0))
    except OSError:
        k32.CloseHandle(wintypes.HANDLE(handle))
        raise


def open_shared(
    path: str | os.PathLike[str],
    mode: str = "r",
    *,
    encoding: str | None = None,
    errors: str | None = None,
) -> IO[Any]:
    """``open(path, mode)`` that never blocks other readers, writers, renames or deletes."""
    path = Path(path)
    win_flags(mode)  # same modes on every platform
    if WINDOWS:
        fd: int | None = None
        try:
            fd = _win_open(path, mode)
        except OSError:  # a real refusal (access denied, missing file): the caller decides
            raise
        except Exception:  # noqa: BLE001 - ctypes trouble: plain open beats no file at all
            fd = None
        if fd is not None:
            return os.fdopen(fd, mode, encoding=encoding, errors=errors)
    return open(path, mode, encoding=encoding, errors=errors)  # noqa: SIM115
