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
EXPLORE = 30  # screens between tries of the way that lost, in case it pays now
# deflate_fast at this rate is not a 68000. Packing before a real sample would
# stick: the line is only timed while replies are raw.
FAST_CPU = 1500.0


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
        given = mode or os.environ.get("AMIGENT_COMPRESS")
        self._given = bool(given)  # by the caller or the environment: link.json doesn't change it
        self.mode = (given or stored.get("mode") or "auto").strip().lower()
        if self.mode not in MODES:
            self.mode = "auto"
        self.rates: dict[str, int] = {}
        try:
            self.line_kbs: float | None = float(stored["kbs"]) if stored.get("kbs") else None
        except (TypeError, ValueError):
            self.line_kbs = None
        self._lock = threading.Lock()
        self._screens: dict[bool, float | None] = {False: None, True: None}  # KB/s unpacked, packed
        self._since_try = EXPLORE  # screens since the losing way was tried: due at once

    def set_mode(self, mode: str) -> None:
        """auto, off, fast or best; kept for this Amiga from now on."""
        mode = mode.strip().lower()
        if mode not in MODES:
            raise ValueError(f"packing is one of {', '.join(MODES)}")
        with _saving:  # a save under way must not read the old mode, then write it back
            self.mode = self.chosen = mode
            self._save(chose=True)

    # what the Amiga said

    def learn(self, sysinfo: dict) -> None:
        for key in ("deflate_fast", "deflate_best", "inflate"):
            try:
                self.rates[key] = int(sysinfo.get(key, 0))
            except ValueError:
                self.rates[key] = 0

    def _needs_probe(self) -> bool:
        """Auto mode, fast CPU, line not timed yet: stay raw so the wire can show its speed."""
        return (self.mode == "auto" and self.line_kbs is None
                and self.rates.get("deflate_fast", 0) >= FAST_CPU)

    @property
    def known(self) -> bool:
        return bool(self.rates)

    # the line

    def note(self, nbytes: int, seconds: float, at_least: bool = False) -> None:
        """nbytes of a reply came in over seconds.

        at_least: the reply came packed, so it waited on the Amiga's packing
        too, and seconds run from when the request went out: it shows only
        that the line carries at least that much. It can raise the estimate,
        never lower it, and never start one."""
        if nbytes < MIN_SAMPLE or seconds <= 0.01:
            return
        kbs = nbytes / 1024 / seconds
        with self._lock:
            if self.line_kbs is None:
                if at_least:
                    return
                self.line_kbs = kbs
            elif at_least and kbs <= self.line_kbs:
                return
            else:
                self.line_kbs = (1 - ALPHA) * self.line_kbs + ALPHA * kbs
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
        if self._needs_probe():
            return 0
        line = self.line()
        if self.rates.get("deflate_best", 0) >= BEST_FACTOR * line:
            return 2
        if self.rates.get("deflate_fast", 0) >= FAST_FACTOR * line:
            return 1
        return 0

    # Screens are measured, not modelled: a screen comes as fast as the Amiga
    # turns it into tiles, so its pace says little about the line. What counts
    # is how fast the tile bytes (as unpacked) arrive each way; the faster way
    # wins. The other way gets one screen in EXPLORE (the first right after the
    # first measurement), whether or not that screen is big enough to measure.

    def frame_level(self) -> int:
        """0, 1 or 2 for the next screen."""
        model = self.level()
        if self.mode != "auto" or not self.known:
            return model
        packed = model or (1 if self.rates.get("deflate_fast") else 0)
        if not packed:
            return 0
        with self._lock:
            plain, squeezed = self._screens[False], self._screens[True]
            if plain is None and squeezed is None:
                return packed if model else 0
            if plain is None or squeezed is None:
                pack_it = squeezed is not None  # the way measured so far
            else:
                pack_it = squeezed > plain
            self._since_try += 1
            if self._since_try > EXPLORE:
                self._since_try = 1
                pack_it = not pack_it
        return packed if pack_it else 0

    def note_frame(self, raw: int, seconds: float, packed: bool) -> None:
        """A screen of raw tile bytes (as unpacked) took seconds, packed or not."""
        if raw < MIN_SAMPLE or seconds <= 0.01:
            return
        kbs = raw / 1024 / seconds
        with self._lock:
            old = self._screens[packed]
            self._screens[packed] = kbs if old is None else (1 - ALPHA) * old + ALPHA * kbs

    def upload(self) -> bool:
        """Pack what goes to the Amiga: unpacking is its part then."""
        if self.mode == "off" or not self.rates.get("inflate"):
            return False
        if self.mode in ("fast", "best"):
            return True
        if self._needs_probe():
            return False
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

    def _save(self, chose: bool = False) -> None:
        with _saving:  # the screen and command connections note from different threads
            try:
                try:
                    everything = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    everything = {}
                if not isinstance(everything, dict):
                    everything = {}
                if not chose:
                    # Another process (the remote desktop, another session) may have set
                    # the mode since this one read it: keep it, and go by it here too.
                    theirs = everything.get(self.name)
                    if isinstance(theirs, dict) and isinstance(theirs.get("mode", ""), str):
                        self._follow(theirs.get("mode", "").strip().lower())
                everything[self.name] = {"kbs": round(self.line_kbs or 0, 1), "mode": self.chosen,
                                         "at": int(time.time())}
                self.path.parent.mkdir(parents=True, exist_ok=True)
                part = self.path.with_name(f"{self.path.name}.{os.getpid()}.part")  # one per process
                try:
                    part.write_text(json.dumps(everything))
                    os.replace(part, self.path)  # a reader never sees half a file
                except OSError:
                    part.unlink(missing_ok=True)  # Windows: another process had link.json open
                    raise
            except OSError:
                pass  # only the next session's first guess is lost

    def _follow(self, chosen: str) -> None:
        """The mode link.json has now, set with set_mode by whoever it was."""
        if chosen == self.chosen or (chosen and chosen not in MODES):
            return  # nobody chose anew: this process's mode stays, also "off" for lack of memory
        self.chosen = chosen
        if not self._given:
            self.mode = chosen or "auto"


_saving = threading.RLock()  # set_mode and saves, from the screen and command threads


_policies: dict[str, Policy] = {}
_registry_lock = threading.Lock()


def policy_for(host: str, name: str | None = None, mode: str | None = None) -> Policy:
    """One policy per Amiga, whoever asks: the agent's channel, its screen, the viewer."""
    key = name or host
    with _registry_lock:
        if key not in _policies:
            _policies[key] = Policy(key, host, mode)
        return _policies[key]
