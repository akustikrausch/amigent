"""One Amiga, many hands: a lock every Amigent process on this machine respects.

The MCP server, the remote desktop and the fleet may all talk to the same
Amiga at once. A file lock in the temp folder keeps them taking turns; it works
on Windows (msvcrt) as well as on macOS and Linux (fcntl).
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
    """Hold the named lock for the with-block; waits as long as someone else has it."""
    with open(path(name), "a+b") as handle:  # append: never truncates whatever sits there
        if os.name == "nt":
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)  # Windows has no blocking flock, so we knock politely
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield  # closing the file drops the lock
