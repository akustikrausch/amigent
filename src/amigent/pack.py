"""Whether packing pays: the Amiga's deflate speed against the line's.

amigentd measures once how fast it packs and unpacks (SYSINFO deflate_fast,
deflate_best, inflate, in KB/s). The PC measures the line from what comes in.
Packing pays when the Amiga packs clearly faster than the line carries: then
the line carries fewer bytes, and the CPU is not what waits. A Pi-based
accelerator or a 68080 behind a plipbox packs a hundred times faster than the
line; a 7 MHz 68000 on a fast emulated network does not, and stays unpacked.

AMIGENT_COMPRESS, or "compress" for an Amiga in amigas.json, decides it by
hand: auto (the default), off, fast or best.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import secret

MODES = ("auto", "off", "fast", "best")
FAST_FACTOR = 2.0  # level 1 when the Amiga packs this many times faster than the line
BEST_FACTOR = 3.0  # level 2 likewise
UNKNOWN_LINE = 100.0  # KB/s to assume before anything was measured: a typical Amiga network
LOCAL_LINE = 20000.0  # KB/s for an emulator on this PC
MIN_SAMPLE = 8192  # bytes; smaller replies say more about latency than about the line
ALPHA = 0.3  # weight of a new measurement


def _file() -> Path:
    return secret.FOLDER / "link.json"


class Policy:
    """Packing decisions for one Amiga; shared by its command and screen connections."""

    def __init__(self, name: str, host: str = "", mode: str | None = None, path: Path | None = None):
        self.name = name or host or "Amiga"
        self.host = host
        self.path = path or _file()
        stored = self._stored()
        self.chosen = stored.get("mode") or ""  # set by hand with set_mode, kept
        self.mode = (mode or os.environ.get("AMIGENT_COMPRESS") or stored.get("mode") or "auto")
        self.mode = self.mode.strip().lower()
        if self.mode not in MODES:
            self.mode = "auto"
        self.rates: dict[str, int] = {}
        try:
            self.line_kbs: float | None = float(stored["kbs"]) if stored.get("kbs") else None
        except (TypeError, ValueError):
            self.line_kbs = None
        self._lock = threading.Lock()

    def set_mode(self, mode: str) -> None:
        """auto, off, fast or best; kept for this Amiga from now on."""
        mode = mode.strip().lower()
        if mode not in MODES:
            raise ValueError(f"packing is one of {', '.join(MODES)}")
        self.mode = self.chosen = mode
        self._save()

    # what the Amiga said

    def learn(self, sysinfo: dict) -> None:
        for key in ("deflate_fast", "deflate_best", "inflate"):
            try:
                self.rates[key] = int(sysinfo.get(key, 0))
            except ValueError:
                self.rates[key] = 0

    @property
    def known(self) -> bool:
        return bool(self.rates)

    # the line

    def note(self, nbytes: int, seconds: float) -> None:
        """nbytes came in over seconds, counted from the first byte of a reply."""
        if nbytes < MIN_SAMPLE or seconds <= 0.01:
            return
        kbs = nbytes / 1024 / seconds
        with self._lock:
            self.line_kbs = kbs if self.line_kbs is None else (1 - ALPHA) * self.line_kbs + ALPHA * kbs
        self._save()

    def line(self) -> float:
        if self.line_kbs:
            return self.line_kbs
        return LOCAL_LINE if self.host.startswith("127.") else UNKNOWN_LINE

    # decisions

    def level(self) -> int:
        """0, 1 or 2 for what comes from the Amiga."""
        if self.mode == "off" or not self.known:
            return 0
        if self.mode == "fast":
            return 1 if self.rates.get("deflate_fast") else 0
        if self.mode == "best":
            return 2 if self.rates.get("deflate_best") else 0
        line = self.line()
        if self.rates.get("deflate_best", 0) >= BEST_FACTOR * line:
            return 2
        if self.rates.get("deflate_fast", 0) >= FAST_FACTOR * line:
            return 1
        return 0

    def upload(self) -> bool:
        """Pack what goes to the Amiga: unpacking is its part then."""
        if self.mode == "off" or not self.rates.get("inflate"):
            return False
        if self.mode in ("fast", "best"):
            return True
        return self.rates["inflate"] >= FAST_FACTOR * self.line()

    def describe(self) -> dict:
        level = self.level()
        return {
            "mode": self.mode,
            "from_amiga": ("off", "fast", "best")[level],
            "to_amiga": "packed" if self.upload() else "off",
            "line_kbs": round(self.line(), 1) if self.line_kbs else None,
            "amiga_packs_kbs": self.rates.get("deflate_fast") or None,
            "amiga_packs_best_kbs": self.rates.get("deflate_best") or None,
            "amiga_unpacks_kbs": self.rates.get("inflate") or None,
        }

    # kept between sessions, per Amiga

    def _stored(self) -> dict:
        try:
            value = json.loads(self.path.read_text()).get(self.name, {})
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def _save(self) -> None:
        try:
            try:
                everything = json.loads(self.path.read_text())
            except (OSError, ValueError):
                everything = {}
            everything[self.name] = {"kbs": round(self.line_kbs or 0, 1), "mode": self.chosen,
                                     "at": int(time.time())}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(everything))
        except OSError:
            pass  # only the next session's first guess is lost


_policies: dict[str, Policy] = {}
_registry_lock = threading.Lock()


def policy_for(host: str, name: str | None = None, mode: str | None = None) -> Policy:
    """One policy per Amiga, whoever asks: the agent's channel, its screen, the viewer."""
    key = name or host
    with _registry_lock:
        if key not in _policies:
            _policies[key] = Policy(key, host, mode)
        return _policies[key]
