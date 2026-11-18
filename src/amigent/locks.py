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
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    @contextmanager
    def _shared(handle):
        # A synchronous shared byte-range lock overlaps the byte that
        # msvcrt.locking() locks exclusively in older versions.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        native = wintypes.HANDLE(msvcrt.get_osfhandle(handle.fileno()))
        offset = _Overlapped()
        if not kernel.LockFileEx(native, 0, 0, 1, 0, ctypes.byref(offset)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not kernel.UnlockFileEx(native, 0, 1, 0, ctypes.byref(offset)):
                raise ctypes.WinError(ctypes.get_last_error())
else:
    import fcntl

    @contextmanager
    def _shared(handle):
        fcntl.flock(handle, fcntl.LOCK_SH)
        yield  # the caller closes the handle


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


@contextmanager
def machine(name: str, *, screen: bool = False):
    """Exclude older global clients and other callers for this machine."""
    suffix = ".screen" if screen else ""
    with open(path("amigent.lock" + suffix), "a+b") as legacy:
        with _shared(legacy), exclusive(name + suffix):
            yield
