"""Did the Amiga start again since the last look, and did it leave a guru behind?

amigentd 1.3.0 reports two uptimes in SYSINFO: `uptime` since the Amiga started
(its E clock) and `daemon_uptime` since amigentd did, plus `last_alert` when
exec kept an alert from before the last reset. After a lost connection this
tells a crash or a reboot from a network fault.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

SLACK = 90  # seconds of link delay that still count as "kept running"
# The E clock of an emulator may run at half speed or slower, so a clock that
# fell behind counts only when it fell far behind: below half of what it should
# show, or below what it showed last time.
SLOW = 0.5


def duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 120:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    return f"{hours // 24} days {hours % 24} h"


def _int(info: dict, key: str) -> int | None:
    try:
        return int(info[key])
    except (KeyError, ValueError):
        return None


def notes(info: dict, before: dict | None, now: float) -> list[str]:
    """What to tell about this SYSINFO compared with the one seen at before["at"]."""
    up, daemon = _int(info, "uptime"), _int(info, "daemon_uptime")
    said = []
    if before and up is not None:
        gone = max(now - before.get("at", now), 0)

        def fell_back(value: int, then: int) -> bool:
            return value < then or value + SLACK < SLOW * (then + gone)

        if fell_back(up, before.get("uptime", 0)):
            said.append(f"the Amiga started again {duration(up)} ago; at the last look "
                        f"{duration(gone)} ago it had been running for "
                        f"{duration(before.get('uptime', 0))}")
        elif daemon is not None and fell_back(daemon, before.get("daemon_uptime", 0)):
            said.append(f"amigentd started again {duration(daemon)} ago (updated or restarted); "
                        "the Amiga itself kept running")
    if info.get("last_alert"):
        said.append(f"exec kept an alert from before the last reset: {info['last_alert'].upper()} "
                    "(a guru meditation code; the first digit 8 means the Amiga could not go on)")
    return said


class Seen:
    """The last SYSINFO uptimes per Amiga, kept in a small file between sessions."""

    def __init__(self, path: Path):
        self.path = path

    def _all(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def check(self, name: str, info: dict, now: float | None = None) -> list[str]:
        """Notes for this look, and remember it for the next one."""
        now = time.time() if now is None else now
        everything = self._all()
        said = notes(info, everything.get(name), now)
        if _int(info, "uptime") is not None:
            everything[name] = {"at": now, "uptime": _int(info, "uptime"),
                                "daemon_uptime": _int(info, "daemon_uptime") or 0}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(everything))
            except OSError:
                pass  # only the next comparison is lost
        return said
