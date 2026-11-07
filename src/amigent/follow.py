"""Copy a growing log from the Amiga to the PC while it grows.

What has reached the PC survives a crash or a hang of the Amiga, also the
part a filesystem such as PFS3 had not yet written to disk: amigentd reads
through the filesystem, which hands out what is still in its buffers. The
debug output (kprintf, Enforcer) is followed the same way.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

DEBUG = "debug"


class Follower:
    """One source, polled every `every` seconds by a thread, appended to `target`.

    fetch(position) returns (new position, new bytes); it raises when the
    Amiga doesn't answer. A position that goes backwards means the file was
    started again, or the Amiga was restarted (debug output).
    """

    def __init__(self, source: str, target: Path, fetch: Callable[[int], tuple[int, bytes]],
                 every: float = 1.0, start: int = 0):
        self.source, self.target, self.fetch, self.every = source, target, fetch, every
        self.position = start
        self.written = 0
        self.lost_since: float | None = None
        self.error = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"follow {source}")

    def start(self) -> "Follower":
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self._mark(f"following {self.source}")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=self.every + 5)
        self._mark(f"stopped following {self.source}")

    def tick(self) -> None:
        """One look; the thread calls it, tests call it directly."""
        try:
            position, data = self.fetch(self.position)
        except Exception as e:  # noqa: BLE001 - a log copier must outlive any failure
            if self.lost_since is None:
                self.lost_since = time.time()
                self.error = str(e) or type(e).__name__
                self._mark(f"the Amiga does not answer: {self.error}")
            return
        if self.lost_since is not None:
            self._mark("the Amiga answers again")
            self.lost_since, self.error = None, ""
        if position < self.position:
            self._mark(f"{self.source} started again")
        if data:
            with open(self.target, "ab") as f:
                f.write(data)
                f.flush()
            self.written += len(data)
        self.position = position

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            self.tick()

    def _mark(self, text: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.target, "ab") as f:
            f.write(f"\n--- {stamp} amigent: {text} ---\n".encode("iso-8859-1", "replace"))


def file_fetch(read_from: Callable[[str, int], bytes], stat: Callable[[str], dict], path: str):
    """fetch for a file: read only what is new; a shorter file starts over."""
    def fetch(position: int) -> tuple[int, bytes]:
        size = int(stat(path).get("size", 0) or 0)
        if size < position:
            position = 0
        if size == position:
            return position, b""
        data = read_from(path, position)
        return position + len(data), data
    return fetch


def debug_fetch(debug_log: Callable[[int], tuple[int, bytes]]):
    """fetch for the debug ring: amigentd counts what went through RawPutChar."""
    def fetch(position: int) -> tuple[int, bytes]:
        total, data = debug_log(position)
        return total, data
    return fetch
