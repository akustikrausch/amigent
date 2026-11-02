"""Which box are we on: WSL wearing a Linux mask, and Windows programs seen from inside it."""

from __future__ import annotations

import os
import platform
import shutil


def in_wsl() -> bool:
    return "microsoft" in platform.release().lower()


def windows_exe(name: str) -> str:
    """A Windows program by name; WSL started from a shortcut may lack the Windows PATH."""
    if found := shutil.which(name):
        return found
    for folder in ("/mnt/c/Windows/System32", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0",
                   "/mnt/c/Windows"):
        if os.path.exists(f"{folder}/{name}"):
            return f"{folder}/{name}"
    return name
