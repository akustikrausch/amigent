"""Watching the Amiga screen against a clock that actually means it.

A FRAME, once asked for, has to be read to its last byte or the picture drifts
out of sync with amigentd. On a 15 KB/s wire a full 1920x1080 frame is a coffee
break, so the poll runs on a worker thread: the caller leaves at its deadline,
the bytes keep trickling in behind its back, and the next screen tool simply
queues behind them on the screen lock.
"""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as Late

PAUSE = 0.25  # between polls of a quiet screen; each one is a round trip anyway


class Lookout:
    """One screen poll at a time, each waited for no longer than the caller can afford."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="amigent-lookout")
        self._busy: Future | None = None
        self.baseline = 0.0  # when a poll without a picture to compare with last finished

    def poll(self, screen, guard, region: tuple, wait: float) -> bool:
        """Poll region within wait seconds; False if it is still running when time is up."""
        end = self.clock() + max(wait, 0.0)
        if self._busy and not self._busy.done():
            try:  # a poll from an earlier, impatient caller still owns the wire
                self._busy.result(timeout=max(end - self.clock(), 0.0))
            except Late:
                return False
            except Exception:
                pass  # its caller has long gone; a broken poll just needs a fresh one
        self._busy = self._pool.submit(self._poll, screen, guard, region)
        try:
            self._busy.result(timeout=max(end - self.clock(), 0.0))
        except Late:
            return False
        return True

    def _poll(self, screen, guard, region):
        cold = screen.snapshot() is None  # no picture: every tile comes, none of them is news
        guard(lambda: screen.poll(*region))
        if cold:
            self.baseline = self.clock()


def wait_change(screen, guard, lookout: Lookout, region: tuple, since: float, timeout: float,
                sleep=time.sleep) -> tuple[str, bool]:
    """Poll region until a tile newer than since shows up, or timeout runs out.

    Returns the report and whether something changed. Never overruns timeout by
    more than a thread hand-over, however slow the wire."""
    clock = lookout.clock
    start = clock()
    deadline = start + timeout
    looked = False  # a poll came back: running out of time later is no slow link
    while True:
        if not lookout.poll(screen, guard, region, deadline - clock()):
            if looked:
                return f"no change within {timeout:g} s", False
            return (f"no change seen within {timeout:g} s; the link is slow and a screen read "
                    "is still on its way, so ask again", False)
        looked = True
        changed = screen.changed_since(max(since, lookout.baseline), *region)
        if changed:
            return f"changed ({changed} tiles) after {clock() - start:.1f} s of waiting", True
        left = deadline - clock()
        if left <= 0:
            return f"no change within {timeout:g} s", False
        sleep(min(PAUSE, left))
