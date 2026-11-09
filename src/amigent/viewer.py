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

from . import __version__, machines
from .amigactl import AmigaError, join_path
from .amigentd import Amigentd
from .channel import Commands, pc_outdated
from .screenfeed import Feed, InputQueue
from .platform_util import in_wsl, windows_exe  # noqa: F401 - windows_exe is imported from here
from .power import flush_volumes, reboot
from .webserver import (MAX_MESSAGE, MAX_UPLOAD, STATIC, WEB, WebSocket,  # noqa: F401 - imported from here
                        _Handler, _Server)

STATE = Path.home() / ".cache" / "amigent" / "viewer.json"
UPLOAD_CHUNK = 32 * 1024
MAX_DOWNLOAD = 16 * 2**20  # about six minutes over a plipbox, with the command line busy
MAX_CLIP = 1 << 20
CLIPBOARD = "CLIPS:0"  # clipboard.device keeps unit 0 there as IFF FTXT


OS_NAMES = {47: "AmigaOS 3.2", 46: "AmigaOS 3.1.4", 45: "AmigaOS 3.9", 44: "AmigaOS 3.5",
            40: "AmigaOS 3.1", 39: "AmigaOS 3.0", 37: "AmigaOS 2.04", 36: "AmigaOS 2.0", 34: "AmigaOS 1.3"}


def describe_system(version: str, cpu: str) -> dict:
    """Turn the output of the Version and CPU commands into a few named facts."""
    kick = re.search(r"Kickstart (\d+)\.(\d+)", version)
    bench = re.search(r"Workbench (\d+\.\d+)", version)
    chips = re.findall(r"\b(680[0-9]0|6888[12])\b", cpu)
    major = int(kick.group(1)) if kick else 0
    return {
        "os": OS_NAMES.get(major, f"Kickstart {major}" if major else ""),
        "kickstart": f"{kick.group(1)}.{kick.group(2)}" if kick else "",
        "workbench": bench.group(1) if bench else "",
        "cpu": next((c for c in chips if not c.startswith("688")), ""),
        "fpu": next((c for c in chips if c.startswith("688")), ""),
    }


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


class Viewer:
    """Streams the screen to every open page and plays their input on the Amiga.

    One worker thread owns the screen connection, so input never races a frame.
    lock() is held around each screen request; the MCP server passes its own so
    the AI agent and the viewer can share one connection and one picture.
    """

    def __init__(self, host: str, screen: Amigentd | None = None, lock=None,
                 ctl_port: int = 6800, screen_port: int = 6801, restart=None,
                 flush_volumes=("SYS:",), remember: bool = False):
        self.host = host
        self.screen = screen or Amigentd(host, screen_port)
        self._thread_lock = threading.Lock()
        self.lock = lock or (lambda: self._thread_lock)
        self.ctl = Commands(host, ctl_port, screen_port)
        self.ctl_lock = threading.Lock()
        self.restart = restart
        self.flush_volumes = list(flush_volumes)
        self.remember = remember
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
        self.ever_joined = False
        self.amiga: dict = {}
        self.screen_list: list = []
        self._system: dict = {}
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
                out.write(json.dumps({"url": self.url, "host": self.host}))
        except OSError:
            pass
        for target in (self.httpd.serve_forever, self.feed.run, self._upload_worker, self._stats):
            threading.Thread(target=target, daemon=True).start()
        return self.url

    # pages

    def join(self, ws: WebSocket) -> None:
        with self.lock():
            picture = self.screen.snapshot()
            ws.send(json.dumps({"type": "hello", "host": machines.name_of(self.host),
                                "address": machines.address(self.host), "online": self.online,
                                "version": __version__, "amiga": self.amiga,
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

    def _watching(self) -> bool:
        with self.clients_lock:
            return bool(self.clients)

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
        self.inputs.put(op)
        self.feed.ring()

    def set_address(self, new: str) -> None:
        """The user says where the Amiga is now."""
        new = new.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:\-]{0,252}", new):
            raise ValueError("not an address")
        machines.move(self.host, new)
        with self.lock():
            self.screen.close()
        with self.ctl_lock:
            self.ctl.close()
        self.event("address", address=new)
        self.feed.ring()

    def _set_online(self, online: bool, reason: str = "") -> None:
        if online != self.online:
            self.online = online
            self.event("online", online=online, reason=reason)

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

    def read_amiga(self) -> dict:
        """Facts about the Amiga that cost it well under a second to tell."""
        if not self._system:
            self._system = describe_system(self._ctl(lambda: self.ctl.exec("Version").output),
                                           self._ctl(lambda: self.ctl.exec("CPU").output))
        info = self._ctl(self.ctl.sysinfo)
        volumes = [{"name": v["name"].rstrip(":"), "free": v["free"], "size": v["capacity"]}
                   for v in self._ctl(self.ctl.volumes)]
        number = lambda key: int(info.get(key, "0") or 0)  # noqa: E731
        return {**self._system, "tcp": info.get("bsdsocket", ""),
                "chip_free": number("chip_free"), "chip_total": number("chip_total"),
                "fast_free": number("fast_free"), "fast_total": number("fast_total"),
                "volumes": sorted(volumes, key=lambda v: v["name"].lower()),
                "pc_outdated": bool(pc_outdated(getattr(getattr(self.ctl, "backend", None), "protocol", 0)))}

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
        self.screen.paused_until = time.monotonic() + left if left else 0.0
        self.event("stats", kbps=0, rtt=round(self.rtt, 1), w=self.screen.width,
                   h=self.screen.height, paused=left)
        self.feed.ring()  # a resume wants a fresh look now, not in five seconds

    def _restart(self) -> None:
        self.event("toast", key="restarting")
        try:
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
        if machines.name_of(state.get("host", "")) != machines.name_of(host):
            return None
        with urllib.request.urlopen(state["url"], timeout=2) as reply:
            return state["url"] if reply.status == 200 else None
    except (OSError, ValueError, KeyError):
        return None


def main() -> None:
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
    host = machines.pick(args.host)
    if not host:
        parser.error("no Amiga known yet: run amigent-setup, or give the Amiga's address")
    if args.install:
        from .desktop import install
        for path in install(machines.name_of(host)):
            print(f"created {path}")
        return
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
    print(f"Amigent viewer: {url}\nCtrl+C to stop.", flush=True)
    if not args.no_window:
        open_window(url)
    try:
        while True:
            time.sleep(1)
            if args.exit_when_closed:
                idle = viewer.unused_for()
                if idle > (15 if viewer.ever_joined else 90):
                    break  # the window is gone; a reload comes back within seconds
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
