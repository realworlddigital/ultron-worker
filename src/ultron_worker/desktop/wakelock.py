"""Keep Windows awake while the runner works, so an overnight ingest doesn't lose hours to
idle sleep. The runner's suspend-aware budget still covers sleeps that happen anyway (lid
closed, power button, battery).

``SetThreadExecutionState`` state belongs to the calling thread, so one long-lived thread (the
app's monitor loop) must make every call. ``ES_SYSTEM_REQUIRED`` only: the display may sleep.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable

log = logging.getLogger("ultron.desktop")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
HOLD = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
RELEASE = ES_CONTINUOUS


def _windows_setter() -> Callable[[int], int] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        fn = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        return None
    fn.argtypes = [ctypes.c_uint]
    fn.restype = ctypes.c_uint
    return fn


class WakeLock:
    """``hold(True)`` while a job is active, ``hold(False)`` when idle; only changes are sent
    to Windows. A no-op off Windows (``supported`` is False)."""

    def __init__(self, setter: Callable[[int], int] | None = None, *, auto: bool = True) -> None:
        self._set = setter if setter is not None else (_windows_setter() if auto else None)
        self.held = False

    @property
    def supported(self) -> bool:
        return self._set is not None

    def hold(self, active: bool) -> bool:
        if active == self.held or self._set is None:
            return self.held
        if self._set(HOLD if active else RELEASE) == 0:
            log.warning(
                "SetThreadExecutionState refused the %s", "wake lock" if active else "release"
            )
            return self.held
        self.held = active
        log.info("wake lock %s", "held: a job is running" if active else "released: idle")
        return self.held

    def release(self) -> None:
        self.hold(False)
