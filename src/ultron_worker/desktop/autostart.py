"""Start Ultron at logon via the per-user Run key (HKCU, no admin, no Scheduled Task)."""

from __future__ import annotations

import subprocess
from typing import Protocol

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "Ultron"


def command_line(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv)


class Registry(Protocol):
    def get(self) -> str | None: ...
    def set(self, value: str) -> None: ...
    def delete(self) -> None: ...


class WinRunKey:
    def __init__(self, name: str = VALUE_NAME) -> None:
        self.name = name

    def get(self) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, self.name)
                return str(value)
        except OSError:
            return None

    def set(self, value: str) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, value)

    def delete(self) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self.name)
        except FileNotFoundError:
            pass


class Autostart:
    def __init__(self, argv: list[str], registry: Registry | None) -> None:
        self.argv = argv
        self.registry = registry  # None: not supported here (not Windows)

    @property
    def supported(self) -> bool:
        return self.registry is not None

    def enabled(self) -> bool:
        return bool(self.registry and self.registry.get())

    def current(self) -> str | None:
        return self.registry.get() if self.registry else None

    def set(self, on: bool) -> bool:
        if self.registry is None:
            raise RuntimeError("autostart is only available on Windows")
        if on:
            self.registry.set(command_line(self.argv))
        else:
            self.registry.delete()
        return self.enabled()

    def refresh(self) -> None:
        """Keep an enabled entry pointing at the current launcher (e.g. after an update)."""
        want = command_line(self.argv)
        if self.registry and self.registry.get() not in (None, want):
            self.registry.set(want)
