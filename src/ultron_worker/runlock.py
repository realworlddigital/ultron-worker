"""One runner per state dir: ``runner.pid`` carries an OS lock for the runner's whole life.

The lock is released by the OS when the process ends, however it ends (a force-kill too), so a
stale file never blocks a start. The locked byte lies past the pid text: Windows locks are
mandatory, and others must still be able to read who holds it.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

LOCK_NAME = "runner.pid"
LOCK_OFFSET = 1 << 20
ALREADY_RUNNING = 4  # the runner's exit code when another runner holds the lock
WINDOWS = os.name == "nt"


def _try_lock(fd: int) -> bool:
    try:
        if WINDOWS:
            import msvcrt

            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if WINDOWS:
            import msvcrt

            os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


def _read_pid(fd: int) -> int:
    with contextlib.suppress(OSError, ValueError):
        os.lseek(fd, 0, os.SEEK_SET)
        return int(os.read(fd, 32).split()[0])
    return -1  # held, but the holder hasn't written its pid yet


class RunnerLock:
    def __init__(self, state_dir: Path) -> None:
        self.path = Path(state_dir) / LOCK_NAME
        self._fd: int | None = None

    def acquire(self, wait: float = 0.0) -> bool:
        """True once held; False if another live process holds it after ``wait`` seconds.
        Raises OSError if the lock file can't be opened at all."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + wait
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.2)  # a just-killed holder: Windows frees its lock a moment later
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            _unlock(fd)
            with contextlib.suppress(OSError):
                os.close(fd)


def holder(state_dir: Path, wait: float = 0.0) -> int | None:
    """The pid of the live runner holding the state dir's lock (-1 if it hasn't written it
    yet), or None if nobody holds it. Waits up to ``wait`` seconds for a holder to let go."""
    path = Path(state_dir) / LOCK_NAME
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        deadline = time.monotonic() + wait
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                return _read_pid(fd)
            time.sleep(0.2)
        _unlock(fd)
        return None
    finally:
        os.close(fd)
