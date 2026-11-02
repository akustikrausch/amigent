"""One lock to rule them all: MCP, viewer and fleet take turns on the same Amiga.

Every Amigent process on this PC honours it. A file lock in the temp folder
does the arbitration, msvcrt on Windows, fcntl on macOS and Linux.
"""

from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def path(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


@contextmanager
def exclusive(name: str):
    """Hold the named lock for the with-block; blocks for as long as someone else has it."""
    with open(path(name), "a+b") as handle:  # append mode: never clobbers what's in there
        if os.name == "nt":
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)  # no blocking flock on Windows: knock every 50 ms, politely
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield  # closing the file drops the lock, no explicit unlock needed
