"""Live screen and manual control of the Amiga in a browser window.

Usage: amigent-viewer [HOST] [--port N] [--no-window] [--exit-when-closed]
       amigent-viewer HOST --install     Start menu and desktop entry on Windows
       amigent-viewer --uninstall

HOST defaults to AMIGENT_HOST. Through MCP, the amiga_viewer tool opens
the same window from the running Amigent server, and a second start of the
viewer opens the window of the one that is already running.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import queue
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zlib
from pathlib import Path

from . import __version__, machines, memory, roster, tls, update
from .amigactl import AmigaError, join_path
from .amigentd import Amigentd, reset_chord
from .channel import LOADED_PROTOCOL, NEWEST_PROTOCOL, Commands, audio_uses_tls, pc_outdated
from .system import (OS_NAMES, apply_loaded, attn_flags, describe_fpu, describe_system,  # noqa: F401
                     exec_before_os3, exec_coldreboot, native_ppc, ppc_software, wants_loaded)
from .screenfeed import Feed, InputQueue
from .platform_util import in_wsl, windows_exe  # noqa: F401 - windows_exe is imported from here
from .power import flush_volumes, reboot
from .webserver import (MAX_MESSAGE, MAX_UPLOAD, STATIC, WEB, WebSocket,  # noqa: F401 - imported from here
                        _Handler, _Server)

STATE = Path.home() / ".cache" / "amigent" / "viewer.json"

# Why the Amiga does not answer, as a key the page translates (locales/*.json).
# Anything not recognised becomes no reason on the page, so a greeting, a path or
# an address never reaches it; the terminal that started the viewer gets the raw text.
CONNECTION_REASONS = {
    "reason_key_wanted": "amigentd wants a key: run amigent-setup, or set AMIGENT_KEY.",
    "reason_key_refused": ("amigentd refused the key; AMIGENT_KEY or the key in ~/.config/amigent/ "
                           "must match S:amigent.key on the Amiga."),
    "reason_closed_by_daemon": "amigentd closed the connection.",
    "reason_stopped_answering": "amigentd stopped answering.",
    "reason_unexpected_greeting": "The Amiga sent an unexpected greeting.",
    "reason_timeout": "The Amiga did not answer in time.",
    "reason_refused": "The Amiga refused the connection.",
    "reason_unreachable": "The Amiga cannot be reached on the network.",
    "reason_closed": "The connection to the Amiga was closed.",
    "reason_unresolved": "The Amiga's address could not be resolved.",
    "reason_plain_closed": tls.PLAIN_CLOSED_TEXT,
    "reason_no_tls": tls.NO_TLS_TEXT,
    "reason_no_certificate": tls.NO_CERT_TEXT,
    "reason_tls_mismatch": "The Amiga's TLS certificate does not match the saved one; run amigent-setup again.",
    "reason_pin_ambiguous": ("Two saved Amigas share this address and their TLS certificates differ; "
                             "fix the address in Your Amigas or run amigent-setup again."),
    "reason_machine_changed": "Machine changed; select it again in Your Amigas.",
}


def connection_reason(text: str) -> str:
    """The key of a known connection failure in text, or '' when there is none."""
    raw = text or ""
    low = raw.lower()
    if "only encrypted connections" in raw:
        return "reason_plain_closed"
    if "amissl does not open" in low:
        return "reason_no_tls"
    if "amigent-tls.pem does not load" in low:
        return "reason_no_certificate"
    if "certificate_verify_failed" in low or "certificate verify failed" in low:
        return "reason_tls_mismatch"
    if "ambiguous tls pin" in low:
        return "reason_pin_ambiguous"
    if "wants a key" in raw:
        return "reason_key_wanted"
    if "refused the key" in raw:
        return "reason_key_refused"
    if "connection closed by amigentd" in raw:
        return "reason_closed_by_daemon"
    if "stopped answering" in raw:
        return "reason_stopped_answering"
    if "unexpected greeting" in raw:
        return "reason_unexpected_greeting"
    if "not answering" not in raw:
        return ""
    if any(part in low for part in ("timed out", "did not properly respond", "failed to respond",
                                    "semaphore timeout")):
        return "reason_timeout"
    if any(part in low for part in ("actively refused", "connection refused", "winerror 10061")):
        return "reason_refused"
    if any(part in low for part in ("no route to host", "network is unreachable", "host is unreachable",
                                    "winerror 10065", "winerror 10051")):
        return "reason_unreachable"
    if any(part in low for part in ("connection reset", "forcibly closed", "connection aborted",
                                    "broken pipe", "winerror 10054", "winerror 10053")):
        return "reason_closed"
    if any(part in low for part in ("name or service not known", "nodename nor servname",
                                    "getaddrinfo", "no such host", "winerror 11001")):
        return "reason_unresolved"
    return ""



# Hear streaming safeguards (product defaults — not the 10 s HW test cap).
AUDIO_DRAIN_MAX = 64  # bounded end-marker / stop drain reads
AUDIO_PCM_BUDGET = 8 * 1024 * 1024  # aggregate PCM broadcast per Hear session


def apollo_machine(info: dict | None) -> bool:
    """Same Apollo identity the Viewer page uses for memory/Hear labelling."""
    if not info:
        return False
    if info.get("os") == "ApolloOS":
        return True
    return "68080" in str(info.get("cpu") or "")


def hear_eligible(info: dict | None, *, connected: bool = True) -> bool:
    """Whether stock Viewer Hear may be shown or start AUDIO.

    Requires an active connection, Apollo identity (not protocol alone),
    protocol ≥ 6, and SYSINFO ``audio=1``. Protocol 6 without Apollo stays hidden.
    """
    if not connected or not info:
        return False
    if not apollo_machine(info):
        return False
    try:
        protocol = int(info.get("daemon_protocol") or 0)
    except (TypeError, ValueError):
        return False
    if protocol < LOADED_PROTOCOL:
        return False
    return info.get("audio") == "1"

UPLOAD_CHUNK = 32 * 1024
MAX_DOWNLOAD = 16 * 2**20  # about six minutes over a plipbox, with the command line busy
MAX_CLIP = 1 << 20
CLIPBOARD = "CLIPS:0"  # clipboard.device keeps unit 0 there as IFF FTXT


def tiles_message(tiles) -> bytes:
    """Binary message 1: x, y, w, h, size and the tile data as amigentd sent it."""
    parts = [b"\x01"]
    for x, y, w, h, data in tiles:
        parts.append(struct.pack(">HHHHI", x, y, w, h, len(data)))
        parts.append(data)
    return b"".join(parts)


def pixels_message(regions) -> bytes:
    """Binary message 3: x, y, w, h and w*h*3 bytes RGB, for pictures the PC decoded (planar)."""
    parts = [b"\x03"]
    for x, y, w, h, rgb in regions:
        parts.append(struct.pack(">HHHH", x, y, w, h))
        parts.append(rgb)
    return b"".join(parts)


_join = join_path


def _rc_out(result) -> tuple[int, str]:
    return result.rc, result.output


def _amiga_name(name: str) -> str:
    name = name.replace("/", "_").replace(":", "_").strip() or "file"
    return name.encode("iso-8859-1", "replace").decode("iso-8859-1").replace("?", "_")[:100]


def ftxt(text: str) -> bytes:
    """Text as the IFF FTXT form the Amiga clipboard holds."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").encode("iso-8859-1", "replace")
    chars = b"CHRS" + struct.pack(">I", len(raw)) + raw + (b"\0" if len(raw) % 2 else b"")
    return b"FORM" + struct.pack(">I", 4 + len(chars)) + b"FTXT" + chars


def ftxt_text(data: bytes) -> str:
    """The text of every CHRS chunk in an FTXT form; anything else gives ''."""
    if len(data) < 12 or data[:4] != b"FORM" or data[8:12] != b"FTXT":
        return ""
    out, at = [], 12
    while at + 8 <= len(data):
        size = struct.unpack(">I", data[at + 4:at + 8])[0]
        if data[at:at + 4] == b"CHRS":
            out.append(data[at + 8:at + 8 + size])
        at += 8 + size + (size & 1)
    return b"".join(out).decode("iso-8859-1")


def reported_bytes(info: dict, key: str) -> int | None:
    """One SYSINFO byte count, or None when the daemon did not send it.

    ``0`` and ``"0"`` are real readings. A missing key, a blank value and a
    value that is not an integer are not.
    """
    if key not in info:
        return None
    raw = info[key]
    if raw is None or isinstance(raw, bool) or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return None
    return int(raw.strip(), 10)


def counted_bytes(info: dict, key: str) -> int:
    """``reported_bytes``, with a missing field read as ``0``.

    Fast RAM and the totals still use this, so an older reply keeps its number.
    """
    value = reported_bytes(info, key)
    return 0 if value is None else value


def status_free_bytes(chip_free, fast_free) -> int:
    """Available Chip RAM plus available Fast RAM.

    An unreported Chip RAM reading adds nothing. A reported zero still does.
    """
    return counted_bytes({"chip_free": chip_free}, "chip_free") + counted_bytes(
        {"fast_free": fast_free}, "fast_free")


def pcm_message(rate: int, pcm: bytes) -> bytes:
    """Binary message 5: s16le stereo from the ARNE mix. rate is samples a second."""
    return struct.pack(">BI", 5, rate) + pcm


class Viewer:
    """Streams the screen to every open page and plays their input on the Amiga.

    One worker thread owns the screen connection, so input never races a frame.
    lock() is held around each screen request; the MCP server passes its own so
    the AI agent and the viewer can share one connection and one picture.
    """

    def __init__(self, host: str, screen: Amigentd | None = None, lock=None,
                 ctl_port: int = 6800, screen_port: int = 6801, restart=None,
                 flush_volumes=("SYS:",), remember: bool = False):
        host = machines.identity(host)
        self.host = host
        self.manager = None
        self.enabled = True
        self.screen = screen or Amigentd(host, screen_port)
        self._thread_lock = threading.Lock()
        self.lock = lock or (lambda: self._thread_lock)
        self.ctl = Commands(host, ctl_port, screen_port)
        self.ctl_lock = threading.Lock()
        self.restart = restart
        self.flush_volumes = list(flush_volumes)
        self.remember = remember
        self.update_done: dict | None = None  # what the last update from this page said, shown once
        self.token = secrets.token_urlsafe(18)
        self.url = ""
        self.httpd: _Server | None = None
        self.clients: set[WebSocket] = set()
        self.clients_lock = threading.Lock()
        self.inputs = InputQueue()
        self.feed = Feed(self.screen, self.lock, self.inputs, watching=self._watching,
                         on_online=self._set_online, on_screens=self._show_screens,
                         on_refused=lambda text: self.event("toast", key="not_supported", text=text),
                         alive=self._answers)
        self.uploads: queue.Queue = queue.Queue()
        self.online = None
        self.reason = ""  # why the Amiga does not answer: a CONNECTION_REASONS key or ""
        self.ever_joined = False
        self.amiga: dict = {}
        self.hear = False  # the page asks; opening the window does not start the mix
        self.screen_list: list = []
        self._system: dict = {}
        self._daemon_path = ""
        self._daemon_bytes: bytes | None = None
        self._daemon_version = ""
        self.alone_since = time.monotonic()
        self.screen.listeners.append(self._on_screen)

    @property
    def rtt(self) -> float:
        return self.feed.rtt

    # lifecycle

    def start(self, port: int = 6810) -> str:
        if self.httpd:
            return self.url
        handler = type("Handler", (_Handler,), {"viewer": self})
        try:
            self.httpd = _Server(("127.0.0.1", port), handler)
        except OSError:
            self.httpd = _Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        try:  # lets a second start open this window instead of fighting over the screen
            if not self.remember:
                raise OSError
            STATE.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w") as out:
                out.write(json.dumps({"url": self.url, "host": self.host,
                                      "direct": isinstance(self.host, roster.DirectAddress)}))
        except OSError:
            pass
        for target in (self.httpd.serve_forever, self.feed.run, self._upload_worker, self._stats,
                       self._media):
            threading.Thread(target=target, daemon=True).start()
        if self.remember:  # the program, not a viewer an agent or the fleet opened
            self.update_done = update.last_run()
            update.refresh_in_background(then=self._update_found)
        return self.url

    # pages

    def join(self, ws: WebSocket) -> None:
        with self.lock():
            picture = self.screen.snapshot()
            ws.send(json.dumps({"type": "hello", "host": machines.name_of(self.host),
                                "address": machines.address(self.host), "online": self.online,
                                "reason": self.reason,
                                "version": __version__, "amiga": self.amiga,
                                "update": update.offer(), "update_done": self.update_done,
                                "screens": self.screen_list,
                                "w": self.screen.width, "h": self.screen.height}))
            if picture is not None:
                out = io.BytesIO()
                picture.save(out, "PNG", compress_level=1)  # palette PNG: a byte a pixel, quick
                ws.send(b"\x02" + out.getvalue())
            with self.clients_lock:
                self.clients.add(ws)
                self.ever_joined = True
        self.feed.ring()

    def leave(self, ws: WebSocket) -> None:
        with self.clients_lock:
            self.clients.discard(ws)
            if not self.clients:
                self.alone_since = time.monotonic()
                self.hear = False

    def _watching(self) -> bool:
        with self.clients_lock:
            return self.enabled and bool(self.clients)

    def unused_for(self) -> float:
        """Seconds without an open page, or 0 while one is open."""
        with self.clients_lock:
            return 0.0 if self.clients else time.monotonic() - self.alone_since

    def broadcast(self, data: bytes | str) -> None:
        with self.clients_lock:
            clients = list(self.clients)
        for ws in clients:
            try:
                ws.send(data)
            except OSError:
                self.leave(ws)

    # a newer Amigent

    def _update_found(self, found: dict | None) -> None:
        if found:
            self.event("update", update=found)

    def update_action(self, action: str) -> dict:
        """The page's answer to "Amigent x is out": later, skip or now."""
        found = update.offer()
        if action == "later":
            return {}
        if not found:
            raise ValueError("no newer Amigent to act on")
        if action == "skip":
            update.skip(found["version"])
            return {}
        if action != "now":
            raise ValueError("later, skip or now")
        if update.how_installed() == "source":
            raise ValueError("this Amigent runs from a source checkout: update it with git pull")
        update.start_for_viewer(self.host)
        # the helper waits for this process to go: close the window's link, then quit
        threading.Timer(1.5, lambda: os._exit(0)).start()
        return {"restarting": True}

    def event(self, kind: str, **fields) -> None:
        self.broadcast(json.dumps({"type": kind, **fields}))

    def _on_screen(self, kind, payload) -> None:
        if kind == "size":
            self.event("size", w=payload[0], h=payload[1])
        elif kind == "tiles":
            self.broadcast(tiles_message(payload))
        elif kind == "planar":  # bitplanes: the c2p happened here, the page gets RGB
            regions = [(x, y, w, h, self.screen.pixels(x, y, w, h)) for x, y, w, h in payload]
            self.broadcast(pixels_message(regions))

    # input: queued here, played by the feed on its pipeline

    def submit(self, op: dict) -> None:
        if not self.enabled:
            return
        if op.get("t") == "restart":
            threading.Thread(target=self._restart, daemon=True).start()
            return
        if op.get("t") == "pause":
            try:
                seconds = max(0, min(int(op.get("seconds", 0)), 0xFFFF))
            except (TypeError, ValueError):
                return  # malformed message from the page
            threading.Thread(target=self._pause, args=(seconds,), daemon=True).start()
            return
        if op.get("t") == "audio":
            want = op.get("on") is True
            if want and not hear_eligible(self.amiga, connected=self.online is True):
                self.hear = False
                self.event("audio", on=False)
                return
            self.hear = want
            return
        self.inputs.put(op)
        self.feed.ring()

    def machine_manager(self):
        if self.manager is None:
            from .fleet import Fleet
            with self.clients_lock:
                if self.manager is None:
                    self.manager = Fleet(machines.load(), existing=self)
                    self.manager.start(0)
        return self.manager

    def set_address(self, new: str) -> str:
        """Connect elsewhere without rewriting the current saved machine."""
        return self.machine_manager().connect(new)

    def retire(self):
        """An address edit or removal invalidates this connection, not its jobs' target."""
        self.enabled = False
        self.reason = "reason_machine_changed"
        self.hear = False  # a retired machine never keeps the mix running
        self.amiga = {}
        self.event("online", online=False, reason=self.reason)
        with self.lock():
            self.screen.close()
        with self.ctl_lock:
            self.ctl.close()

    def _set_online(self, online: bool, reason: str = "") -> None:
        shown = "" if online else connection_reason(reason)
        if online != self.online or shown != self.reason:
            if not online:
                self.hear = False
                self.amiga = {}
            self.online = online
            self.reason = shown
            self.event("online", online=online, reason=self.reason)
            # stderr: the terminal that started amigent-viewer (stdout is the MCP server's protocol)
            stamp = time.strftime("%H:%M:%S")
            if online:
                print(f"{stamp} connected to {machines.name_of(self.host)}", file=sys.stderr, flush=True)
            else:
                print(f"{stamp} the Amiga does not answer: {reason or 'no reason given'}",
                      file=sys.stderr, flush=True)

    def _stats(self) -> None:
        seconds = 0
        while True:
            before = self.screen.received
            time.sleep(1)
            with self.clients_lock:
                watching = bool(self.clients)
            if not watching:
                continue
            self.event("stats", kbps=round((self.screen.received - before) / 1024, 1),
                       rtt=round(self.rtt, 1), w=self.screen.width, h=self.screen.height,
                       paused=self.screen.pause_left(ask=False))
            if self.online and seconds % 2 == 0:
                self._read_screens()
            if self.online and (not self.amiga or seconds % 30 == 0):
                try:
                    self.amiga = self.read_amiga()
                    self.event("amiga", **self.amiga)
                except (AmigaError, OSError):
                    pass  # the next round tries again
            seconds += 1

    def _hearing(self) -> bool:
        with self.clients_lock:
            return bool(self.hear and self.clients)

    def _audio_ready(self) -> bool:
        """Whether this connection may stream Hear (Apollo + protocol ≥ 6 + audio=1)."""
        if hear_eligible(self.amiga, connected=self.online is True):
            return True
        if self.hear:
            self.hear = False
            self.event("audio", on=False)
        return False

    def _release_audio(self, link: Commands) -> None:
        try:
            if link.connected:
                try:
                    link.audio_stop()
                    for _ in range(AUDIO_DRAIN_MAX):
                        if link.audio_read() is None:
                            break
                except (AmigaError, OSError, AttributeError):
                    pass
        finally:
            try:
                link.close()
            except (AmigaError, OSError, AttributeError):
                pass
            self.event("audio", on=False)

    def _play_audio(self, link: Commands, between=None) -> None:
        """Stream the ARNE mix while the page keeps asking. ``between`` runs after each block.

        A daemon end-marker, PCM budget, or Hear OFF always releases the stream and
        clears ``hear`` so ``_media`` cannot silently call ``audio_start`` again.
        The user must click Hear to resume.
        """
        if not link.connected:
            link.connect_audio(tls=audio_uses_tls(getattr(self.screen, "flags", 0), self.host))
        link.audio_start()
        self.event("audio", on=True)
        pcm_sent = 0
        try:
            while self._hearing():
                got = link.audio_read()
                if got is None:
                    break
                rate, pcm = got
                if pcm and len(pcm) % 4:
                    raise AmigaError("audio samples are not stereo frames")
                if pcm:
                    room = AUDIO_PCM_BUDGET - pcm_sent
                    if room < 4:
                        break
                    if len(pcm) > room:
                        pcm = pcm[: room - (room % 4)]
                    if pcm:
                        self.broadcast(pcm_message(rate or 48000, pcm))
                        pcm_sent += len(pcm)
                if between:
                    between()
        finally:
            # Never return to _media with hear still True after a stream attempt.
            self.hear = False
            self._release_audio(link)

    def _drop_audio(self, link: Commands) -> None:
        self.hear = False
        try:
            if link.connected:
                try:
                    link.audio_stop()
                    for _ in range(AUDIO_DRAIN_MAX):
                        if link.audio_read() is None:
                            break
                except (AmigaError, OSError, AttributeError):
                    pass
        finally:
            try:
                link.close()
            except (AmigaError, OSError, AttributeError):
                pass
            self.event("audio", on=False)

    def _media(self) -> None:
        """ARNE audio only after the page asks, while a page is open.

        AUDIO is opcode 48 on its own command channel. The screen connection is
        not this socket. When the daemon has closed the plain port, this socket
        is TLS. A failed TLS handshake is not retried in the clear. Opening the
        window never starts the mix.
        """
        link = Commands(self.host, self.ctl.ctl_port, self.ctl.screen_port, timeout=8)

        while True:
            if not self._watching():
                self.hear = False
                if link.connected:
                    self._release_audio(link)
                time.sleep(0.05)
                continue
            if self._hearing() and self._audio_ready():
                try:
                    self._play_audio(link)
                except (AmigaError, AttributeError, OSError):
                    self._drop_audio(link)
                    time.sleep(0.05)
                continue
            if link.connected:
                self._release_audio(link)
            time.sleep(0.05)

    def _read_screens(self) -> None:
        """Ask for the screen list on the feed's pipeline; no round trip of its own."""
        if self.screen.protocol >= 2:
            self.feed.want_screens = True

    def _show_screens(self, found: list) -> None:
        """Tell the pages about the Amiga's screens when the list changes."""
        listing = [{"handle": s["handle"], "title": s["title"], "front": s["front"],
                    "w": s["width"], "h": s["height"]} for s in found]
        if listing != self.screen_list:
            self.screen_list = listing
            self.event("screens", screens=listing)

    def _graphics(self) -> str:
        """The card's driver, and on a Zorro II card a gentler screen loop (#22)."""
        from . import graphics
        try:
            loaded = memory.parse(self._ctl(self.ctl.loaded))
            info = graphics.detect(loaded, lambda a, n: self._ctl(lambda: self.ctl.peek(a, n)))
        except (AmigaError, AttributeError, TypeError, ValueError):
            return ""  # amigentd before 1.4.0: the loop stays as it was
        if feed := getattr(self, "feed", None):  # a half-built viewer in tests has none
            feed.planner.slow_bus = info.slow_bus
        return ", ".join(f"{c[0]} {c[1]}" for c in info.cards)

    def read_amiga(self) -> dict:
        """Facts about the Amiga that cost it well under a second to tell."""
        if not self._system:
            self._system = describe_system(self._ctl(lambda: self.ctl.exec("Version").output),
                                           self._ctl(lambda: self.ctl.exec("CPU").output))
            if not native_ppc(self._system):
                ppc = ppc_software(lambda c: self._ctl(lambda: _rc_out(self.ctl.exec(c))))
                if ppc:
                    self._system["ppc"] = ppc
            daemon = re.search(r"amigentd (\d[\w.]*)", self._ctl(lambda: self.ctl.exec("Version C:amigentd").output) or "")
            if daemon:
                self._system["amigentd"] = daemon.group(1)
        info = self._ctl(self.ctl.sysinfo)
        if not native_ppc(self._system) and "processor" not in self._system:
            # exec's AttnFlags tell the FPU better than C:CPU's text; amiga_status adds the library check
            fpu = describe_fpu(attn_flags(info), self._system.get("cpu", ""), self._system.get("fpu", ""))
            if fpu.support_missing or (fpu.present is False and wants_loaded(fpu)):
                try:  # Emu68 runs the FPU itself; only its resource in LOADED tells
                    apply_loaded(fpu, memory.parse(self._ctl(self.ctl.loaded)).libraries)
                except (AmigaError, AttributeError):
                    pass
            self._system.update(fpu=fpu.short(), processor=fpu.line(), fpu_state=fpu.state(),
                                fpu_cpu=fpu.cpu, fpu_chip=fpu.kind if fpu.state() == "chip" else "",
                                fpu_library=fpu.library)
        if "graphics" not in self._system:
            self._system["graphics"] = self._graphics()
        volumes = [{"name": v["name"].rstrip(":"), "free": v["free"], "size": v["capacity"]}
                   for v in self._ctl(self.ctl.volumes)]
        facts = dict(self._system)
        if info.get("os"):
            facts["os"] = info["os"]
        if str(info.get("cpu") or "") == "68080" or info.get("os") == "ApolloOS":
            facts["cpu"] = "68080"
        # chip_free stays None when SYSINFO omits it, so a page can tell a real
        # zero from an older daemon. The other counts keep "missing means 0".
        protocol = getattr(getattr(self.ctl, "backend", None), "protocol", 0) or self.screen.protocol
        return {**facts, "daemon_protocol": protocol,
                "tcp": info.get("bsdsocket", ""),
                "board": info.get("board", ""), "board_name": info.get("board_name", ""),
                "audio": info.get("audio", ""),
                "chip_free": reported_bytes(info, "chip_free"),
                "chip_total": counted_bytes(info, "chip_total"),
                "fast_free": counted_bytes(info, "fast_free"),
                "fast_total": counted_bytes(info, "fast_total"),
                "volumes": sorted(volumes, key=lambda v: v["name"].lower()),
                "pc_outdated": bool(pc_outdated(getattr(getattr(self.ctl, "backend", None), "protocol", 0))),
                "link": self.link()}

    def link(self) -> dict:
        """How the picture travels, for the page's Connection tab: no round trip needed."""
        screen = getattr(self, "screen", None)  # a half-built viewer in tests has none
        protocol = int(getattr(screen, "protocol", 0) or 0)
        return {"port": getattr(screen, "port", 0), "protocol": protocol,
                "tls": hasattr(getattr(screen, "_sock", None), "version"),
                "amiga_outdated": 0 < protocol < NEWEST_PROTOCOL}


    def _answers(self) -> bool:
        """Is the Amiga there? Asked on the command line, which leaves the screen alone."""
        try:
            self._ctl(lambda: self.ctl.stat("RAM:"))
            return True
        except Exception:  # noqa: BLE001 - any failure means no
            return False

    # files

    def _ctl(self, fn):
        with self.ctl_lock:
            if not self.enabled:
                raise AmigaError("Machine changed; select it again in Your Amigas.")
            if not self.ctl.connected:
                self.ctl.connect()
            try:
                return fn()
            except (OSError, EOFError) as e:
                self.ctl.close()
                raise AmigaError(f"amigactld connection lost: {e}") from e

    def folders(self, path: str) -> dict:
        volumes = sorted(v["name"].rstrip(":") + ":" for v in self._ctl(self.ctl.volumes))
        folders = []
        if path:
            entries = self._ctl(lambda: self.ctl.list_dir(path))
            folders = sorted((e["name"] for e in entries  # soft links to icons show up as DIR
                              if e["type"].upper() == "DIR" and not e["name"].lower().endswith(".info")),
                             key=str.lower)
        files = []
        if path:
            files = sorted(({"name": e["name"], "size": e["size"]} for e in entries
                            if e["type"].upper() == "FILE" and not e["name"].lower().endswith(".info")),
                           key=lambda f: f["name"].lower())
        return {"path": path, "volumes": volumes, "folders": folders, "files": files}

    def download(self, path: str) -> bytes:
        size = int(self._ctl(lambda: self.ctl.stat(path)).get("size", 0) or 0)
        if size > MAX_DOWNLOAD:
            raise AmigaError(f"{path} is larger than {MAX_DOWNLOAD >> 20} MB")
        return self._ctl(lambda: self.ctl.read_file(path))

    def read_clipboard(self) -> str:
        try:
            size = int(self._ctl(lambda: self.ctl.stat(CLIPBOARD)).get("size", 0) or 0)
        except AmigaError as e:
            if e.code is None:
                raise
            return ""  # nothing copied yet
        if size > MAX_CLIP:
            raise AmigaError("the Amiga's clipboard holds more than text")  # a picture, say
        return ftxt_text(self._ctl(lambda: self.ctl.read_file(CLIPBOARD)))

    def write_clipboard(self, text: str) -> None:
        self._ctl(lambda: self.ctl.write_file(CLIPBOARD, ftxt(text)))

    def queue_upload(self, folder: str, name: str, data: bytes, overwrite: bool) -> dict:
        path = _join(folder.strip() or "RAM:", _amiga_name(name))
        if not overwrite:
            try:
                self._ctl(lambda: self.ctl.stat(path))
                return {"exists": True, "path": path}
            except AmigaError as e:
                if e.code is None:
                    raise
        job = {"id": secrets.token_hex(4), "path": path, "size": len(data)}
        self.uploads.put((job, data))
        self.event("upload", state="queued", **job)
        return job

    def _upload_worker(self) -> None:
        while True:
            job, data = self.uploads.get()
            try:
                done = 0
                while True:
                    chunk = data[done:done + UPLOAD_CHUNK]
                    self._ctl(lambda: self.ctl.write_file(job["path"], chunk, append=done > 0))
                    done += len(chunk)
                    self.event("upload", state="sending", done=done, **job)
                    if done >= len(data):
                        break
                if self._ctl(lambda: self.ctl.checksum(job["path"])) != zlib.crc32(data):
                    raise AmigaError("checksum differs after the transfer")
                self.event("upload", state="done", done=done, **job)
            except (AmigaError, OSError) as e:
                self.event("upload", state="failed", error=str(e), **job)

    def _pause(self, seconds: int) -> None:
        """Stop the Amiga reading its screen for a while, or let it go on (0)."""
        try:
            left = self._ctl(lambda: self.ctl.pause_screen(seconds))
        except (AmigaError, OSError, AttributeError) as e:  # AttributeError: amigactld only
            self.event("toast", key="pause_failed", text=str(e))
            return
        self.screen.note_pause(left)
        self.event("stats", kbps=0, rtt=round(self.rtt, 1), w=self.screen.width,
                   h=self.screen.height, paused=left)
        self.feed.ring()  # a resume wants a fresh look now, not in five seconds

    def _exec_version(self) -> str:
        """The exec the daemon is running, as '37.175'. Kickstart from Version if SYSINFO has none."""
        info = self._ctl(self.ctl.sysinfo)
        version = str(info.get("exec_version") or "")
        return version or str(self.amiga.get("kickstart") or "")

    def _reboot_missing(self) -> bool:
        """True when C:Reboot is absent. A dead connection is raised, not treated as missing."""
        try:
            self._ctl(lambda: self.ctl.stat("C:Reboot"))
        except AmigaError as exc:
            if exc.code is None:
                raise
            return True
        return False

    def _submit_reset_chord(self) -> None:
        """Ctrl and both Amiga keys, down together, on the screen connection."""
        for op in reset_chord():
            self.inputs.put(op)
        self.feed.ring()

    def _restart(self) -> None:
        self.event("toast", key="restarting")
        try:
            # Workbench 3.0 added C:Reboot. Exec 36, 37 and 38 still reset
            # from the keyboard chord. Exec below 36 is not sent the chord:
            # the three key-downs have no key-up, and ColdReboot() is absent,
            # so the modifiers would stay down on a machine that does not reset.
            # The daemon still requires the three keys inside five seconds.
            if self._reboot_missing():
                version = self._exec_version()
                if exec_before_os3(version):
                    if not exec_coldreboot(version):
                        raise AmigaError(
                            f"unsupported operation: exec {version} cannot ColdReboot() "
                            "and has no C:Reboot")
                    if not self.online:
                        raise AmigaError("the Amiga is not connected")
                    self._ctl(lambda: self.ctl.flush(self.flush_volumes))
                    self._submit_reset_chord()
                    return
            if self.restart:
                self.restart()
            else:
                self._ctl(lambda: reboot(self.ctl, self.flush_volumes))
        except (AmigaError, OSError) as e:
            self.event("toast", key="restart_failed", text=str(e))


def open_window(url: str) -> None:
    """Show url as its own window (Edge or Chrome app mode), else in the default browser."""
    app = [f"--app={url}", "--window-size=1440,900", "--new-window"]
    wsl = in_wsl()
    commands = []
    if wsl or sys.platform == "win32":
        cmd = windows_exe("cmd.exe") if wsl else "cmd"
        commands.append([cmd, "/c", "start", "", "msedge", *app])
    elif sys.platform == "darwin":
        for browser in ("Google Chrome", "Microsoft Edge", "Chromium"):
            for folder in (Path("/Applications"), Path.home() / "Applications"):
                if (folder / f"{browser}.app").exists():
                    commands.append(["open", "-na", str(folder / f"{browser}.app"), "--args", *app])
                    break
    else:
        for browser in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            if shutil.which(browser):
                commands.append([browser, *app])
    cwd = "/mnt/c" if wsl and os.path.isdir("/mnt/c") else None  # cmd.exe dislikes UNC paths
    for command in commands:
        try:
            subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue
    if wsl:
        subprocess.Popen([windows_exe("explorer.exe"), url], cwd=cwd, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return
    webbrowser.open(url)


def focus_window(title: str) -> bool:
    """Bring the browser window whose title starts with title to the front."""
    try:
        if sys.platform == "darwin":
            for browser in ("Google Chrome", "Microsoft Edge", "Chromium"):
                if not any((folder / f"{browser}.app").exists()
                           for folder in (Path("/Applications"), Path.home() / "Applications")):
                    continue  # osascript would ask where a missing program is
                script = (f'if application "{browser}" is running then\n'
                          f'tell application "{browser}"\n'
                          f'repeat with w in windows\n'
                          f'if title of w starts with "{title}" then\n'
                          f'set index of w to 1\nactivate\nreturn "ok"\nend if\n'
                          f'end repeat\nend tell\nend if\nreturn ""')
                done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                                      timeout=10)
                if done.stdout.strip() == "ok":
                    return True
            return False
        if sys.platform == "win32" or in_wsl():
            exe = windows_exe("powershell.exe") if sys.platform != "win32" else "powershell"
            title_ps = title.replace("'", "''")
            done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command",
                                   f"(New-Object -ComObject WScript.Shell).AppActivate('{title_ps}')"],
                                  capture_output=True, text=True, timeout=15,
                                  cwd="/mnt/c" if os.path.isdir("/mnt/c") else None)
            return done.stdout.strip().lower() == "true"
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def _pages(url: str) -> int:
    """How many pages the running viewer at url has open."""
    try:
        api = url.replace("/?t=", "/api/clients?t=")
        with urllib.request.urlopen(api, timeout=2) as reply:
            return int(json.loads(reply.read()).get("clients", 0))
    except (OSError, ValueError):
        return 0


def show(url: str, host: str) -> None:
    """Bring the viewer's window up: the open one if there is one, else a new one."""
    if not (_pages(url) and focus_window(f"Amigent · {machines.name_of(host)}")):
        open_window(url)


def running_viewer(host: str) -> str | None:
    """The address of a viewer for host that already runs on this PC, if any."""
    try:
        state = json.loads(STATE.read_text())
        saved_host = state.get("host", "")
        if state.get("direct"):
            saved_host = roster.DirectAddress(saved_host)
        if isinstance(saved_host, roster.DirectAddress) != isinstance(host, roster.DirectAddress):
            return None
        if machines.identity(saved_host) != machines.identity(host):
            return None
        with urllib.request.urlopen(state["url"], timeout=2) as reply:
            return state["url"] if reply.status == 200 else None
    except (OSError, ValueError, KeyError):
        return None


def _main() -> None:
    parser = argparse.ArgumentParser(prog="amigent-viewer", description="Watch and drive the Amiga.")
    parser.add_argument("host", nargs="?", default="",
                        help="the Amiga's name or address (default: AMIGENT_HOST, else the known Amiga)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMIGENT_VIEWER_PORT", "6810")))
    parser.add_argument("--no-window", action="store_true", help="only print the address")
    parser.add_argument("--exit-when-closed", action="store_true",
                        help="stop once the window has been closed")
    parser.add_argument("--install", action="store_true",
                        help="install Amigent as a program (Windows: Start menu, desktop, Installed apps; macOS: ~/Applications)")
    parser.add_argument("--uninstall", action="store_true", help="remove those shortcuts again")
    parser.add_argument("--version", action="version", version=f"Amigent {__version__}")
    args = parser.parse_args()
    if args.uninstall:
        from .desktop import uninstall
        for path in uninstall():
            print(f"removed {path}")
        return
    explicit = args.host or os.environ.get("AMIGENT_HOST", "")
    if args.install:
        from .desktop import install
        for path in install(machines.identity(explicit) if explicit else ""):
            print(f"created {path}")
        return
    known = machines.load()
    if not explicit and len(known) != 1:
        from .fleet import Fleet
        overview = Fleet(known)
        url = overview.start(args.port)
        print(f"Your Amigas: {url}\nCtrl+C to stop.", flush=True)
        if not args.no_window:
            open_window(url)
        try:
            while True:
                time.sleep(1)
                if args.exit_when_closed and overview.unused_for() > (15 if overview.ever_joined else 90):
                    break
        except KeyboardInterrupt:
            pass
        return
    host = machines.identity(machines.pick(explicit))
    selected = machines.find(explicit) if explicit else None
    if explicit and (not selected or explicit.casefold() == selected["host"].casefold()):
        host = roster.DirectAddress(machines.validate_address(explicit))
    if url := running_viewer(host):
        if not args.no_window:
            show(url, host)
        print(f"Amigent viewer already running: {url}", flush=True)
        return
    viewer = Viewer(
        host,
        ctl_port=int(os.environ.get("AMIGENT_CTL_PORT", "6800")),
        screen_port=int(os.environ.get("AMIGENT_SCREEN_PORT", "6801")),
        flush_volumes=flush_volumes(),
        remember=True,
    )
    url = viewer.start(args.port)
    address = machines.address(host) or host
    print(f"Amigent viewer: {url}\nConnecting to {machines.name_of(host)} at {address}, "
          f"screen port {viewer.screen.port}; Ctrl+C to stop.", flush=True)
    if not args.no_window:
        open_window(url)
    try:
        while True:
            time.sleep(1)
            if args.exit_when_closed:
                owner = viewer.manager or viewer
                idle = owner.unused_for()
                if idle > (15 if owner.ever_joined else 90):
                    break  # the window is gone; a reload comes back within seconds
    except KeyboardInterrupt:
        pass




def main() -> None:
    from .roster import RegistryError
    try:
        _main()
    except RegistryError as e:  # a damaged amigas.json: say what is wrong, not a traceback
        raise SystemExit(f"amigent-viewer: {e}") from None


if __name__ == "__main__":
    main()
