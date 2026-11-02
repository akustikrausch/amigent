"""Live screen and manual control of the Amiga in a browser window.

Usage: amigent-viewer [HOST] [--port N] [--no-window] [--exit-when-closed]
       amigent-viewer HOST --install     Start menu and desktop entry on Windows
       amigent-viewer --uninstall

HOST defaults to AMIGENT_HOST. Inside Claude Code the amiga_viewer tool opens
the same window from the running amigent server, and a second start of the
viewer opens the window of the one that is already running.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import platform
import queue
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .amigactl import AmigaCtl, AmigaError
from .amigentd import Amigentd, AmigentdError

WEB = Path(__file__).with_name("web")
STATIC = {"viewer.js": "text/javascript", "viewer.css": "text/css", "icon.svg": "image/svg+xml",
          "icon.png": "image/png", "amigent.ico": "image/x-icon"}
STATE = Path.home() / ".cache" / "amigent" / "viewer.json"
TILE = 64
BAND = 4 * TILE  # rows per poll; an unchanged band costs the Amiga about 35 ms
POINTER_BOX = 3 * TILE
ACTIVE_SECS = 3  # after input, poll around the pointer between bands
UPLOAD_CHUNK = 32 * 1024
MAX_UPLOAD = 512 * 2**20
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WHEEL_UP, WHEEL_DOWN = 0x7A, 0x7B  # NewMouse convention
REPEAT = 0x0200


class WebSocket:
    """Just enough RFC 6455 for one local page: unfragmented sends, masked receives."""

    def __init__(self, reader, sock: socket.socket):
        self.reader = reader
        self.sock = sock
        self._send_lock = threading.Lock()

    @staticmethod
    def accept_key(key: str) -> str:
        return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()

    def send(self, data: bytes | str, opcode: int | None = None) -> None:
        if isinstance(data, str):
            data, opcode = data.encode(), opcode or 1
        n = len(data)
        head = bytes([0x80 | (opcode or 2)])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += struct.pack(">BH", 126, n)
        else:
            head += struct.pack(">BQ", 127, n)
        with self._send_lock:
            self.sock.sendall(head + data)

    def receive(self) -> str | bytes | None:
        """Next message, or None when the page closed the socket."""
        message, kind = b"", None
        while True:
            b1, b2 = self._exact(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            mask = self._exact(4) if b2 & 0x80 else b""
            payload = self._exact(n)
            if mask and n:
                key = (mask * (n // 4 + 1))[:n]
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
            opcode = b1 & 0x0F
            if opcode == 8:
                return None
            if opcode == 9:
                self.send(payload, 10)
                continue
            if opcode == 10:
                continue
            kind = kind or opcode
            message += payload
            if b1 & 0x80:
                return message.decode("utf-8", "replace") if kind == 1 else message

    def _exact(self, n: int) -> bytes:
        data = self.reader.read(n) if n else b""
        if len(data) != n:
            raise EOFError
        return data

    def close(self) -> None:
        try:
            self.send(b"", 8)
        except OSError:
            pass


def tiles_message(tiles) -> bytes:
    """Binary message 1: x, y, w, h, size and the tile data as amigentd sent it."""
    parts = [b"\x01"]
    for x, y, w, h, data in tiles:
        parts.append(struct.pack(">HHHHI", x, y, w, h, len(data)))
        parts.append(data)
    return b"".join(parts)


def _join(folder: str, name: str) -> str:
    return folder + name if folder.endswith((":", "/")) else f"{folder}/{name}"


def _amiga_name(name: str) -> str:
    name = name.replace("/", "_").replace(":", "_").strip() or "file"
    return name.encode("iso-8859-1", "replace").decode("iso-8859-1").replace("?", "_")[:100]


class Viewer:
    """Streams the screen to every open page and plays their input on the Amiga.

    One worker thread owns the screen connection, so input never races a frame.
    lock() is held around each screen request; the MCP server passes its own so
    Claude and the viewer can share one connection and one picture.
    """

    def __init__(self, host: str, screen: Amigentd | None = None, lock=None,
                 ctl_port: int = 6800, screen_port: int = 6801, restart=None,
                 flush_volumes=("SYS:",), remember: bool = False):
        self.host = host
        self.screen = screen or Amigentd(host, screen_port)
        self._thread_lock = threading.Lock()
        self.lock = lock or (lambda: self._thread_lock)
        self.ctl = AmigaCtl(host, ctl_port)
        self.ctl_lock = threading.Lock()
        self.restart = restart
        self.flush_volumes = list(flush_volumes)
        self.remember = remember
        self.token = secrets.token_urlsafe(18)
        self.url = ""
        self.httpd: ThreadingHTTPServer | None = None
        self.clients: set[WebSocket] = set()
        self.clients_lock = threading.Lock()
        self.inputs: list[dict] = []
        self.inputs_lock = threading.Lock()
        self.wake = threading.Event()
        self.uploads: queue.Queue = queue.Queue()
        self.online = None
        self.ever_joined = False
        self.alone_since = time.monotonic()
        self.pointer = (0, 0)
        self.last_input = 0.0
        self.received = 0
        self.rtt = 0.0
        self.screen.listeners.append(self._on_screen)

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
            STATE.write_text(json.dumps({"url": self.url, "host": self.host}))
            STATE.chmod(0o600)
        except OSError:
            pass
        for target in (self.httpd.serve_forever, self._work, self._upload_worker, self._stats):
            threading.Thread(target=target, daemon=True).start()
        return self.url

    # pages

    def join(self, ws: WebSocket) -> None:
        with self.lock():
            picture = self.screen.snapshot()
            ws.send(json.dumps({"type": "hello", "host": self.host, "online": self.online,
                                "w": self.screen.width, "h": self.screen.height}))
            if picture is not None:
                out = io.BytesIO()
                picture.save(out, "PNG", compress_level=1)
                ws.send(b"\x02" + out.getvalue())
            with self.clients_lock:
                self.clients.add(ws)
                self.ever_joined = True
        self.wake.set()

    def leave(self, ws: WebSocket) -> None:
        with self.clients_lock:
            self.clients.discard(ws)
            if not self.clients:
                self.alone_since = time.monotonic()

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
            message = tiles_message(payload)
            self.received += len(message)
            self.broadcast(message)

    # input

    def submit(self, op: dict) -> None:
        if op.get("t") == "restart":
            threading.Thread(target=self._restart, daemon=True).start()
            return
        with self.inputs_lock:
            if op.get("t") == "move" and self.inputs and self.inputs[-1].get("t") == "move":
                self.inputs[-1] = op  # only the newest position matters
            else:
                self.inputs.append(op)
        self.last_input = time.monotonic()
        self.wake.set()

    def _play(self, op: dict) -> None:
        s, kind = self.screen, op.get("t")
        if kind == "move":
            x = max(0, min(int(op["x"]), s.width - 1))
            y = max(0, min(int(op["y"]), s.height - 1))
            start = time.perf_counter()
            s.move(x, y)
            self.rtt = self.rtt * 0.8 + (time.perf_counter() - start) * 200  # ms, smoothed
            self.pointer = (x, y)
        elif kind == "buttons":
            s.set_buttons(int(op["mask"]))
        elif kind == "key":
            q = int(op.get("q", 0)) | (REPEAT if op.get("repeat") else 0)
            s.rawkey(int(op["code"]), bool(op["down"]), q)
        elif kind == "char":
            s.char(str(op["c"])[:1], int(op.get("q", 0)))
        elif kind == "text":
            s.type_text(str(op["s"])[:20000].replace("\r\n", "\n"))
        elif kind == "combo":
            s.press(str(op["k"]))
        elif kind == "wheel":
            code = WHEEL_UP if int(op["dy"]) < 0 else WHEEL_DOWN
            for _ in range(min(abs(int(op["dy"])), 5)):
                s.rawkey(code, True)
                s.rawkey(code, False)

    def _play_inputs(self) -> None:
        with self.inputs_lock:
            ops, self.inputs = self.inputs, []
        if ops:
            with self.lock():
                for op in ops:
                    try:
                        self._play(op)
                    except (KeyError, ValueError, TypeError):
                        pass  # malformed message from the page

    # screen

    def _regions(self):
        """Bands top to bottom, with the area around the pointer in between while in use."""
        band = 0
        while True:
            height = max(self.screen.height, 1)
            if band * BAND >= height:
                band = 0
            if band == 0:
                yield "size"
            yield (0, band * BAND, self.screen.width, BAND)
            band += 1
            if time.monotonic() - self.last_input < ACTIVE_SECS:
                px, py = self.pointer
                x = max(0, min(px // TILE * TILE - TILE, self.screen.width - POINTER_BOX))
                y = max(0, min(py // TILE * TILE - TILE, height - POINTER_BOX))
                yield (x, y, POINTER_BOX, POINTER_BOX)

    def _work(self) -> None:
        regions = self._regions()
        quiet = 0
        while True:
            with self.clients_lock:
                watching = bool(self.clients)
            if not watching:
                self.wake.wait(1)
                self.wake.clear()
                continue
            try:
                self._play_inputs()
                region = next(regions)
                start = time.perf_counter()
                with self.lock():
                    if region == "size":
                        self.screen.check_size()
                        changed = 0
                    else:
                        changed = self.screen.poll(*region)
                spent = time.perf_counter() - start
                self._set_online(True)
            except (OSError, AmigentdError, EOFError) as e:
                self.screen.close()
                self._set_online(False, str(e))
                self.wake.wait(2)
                self.wake.clear()
                continue
            # Keep the Amiga's share for its own programs: wait as long as the poll took,
            # and longer while nothing happens on either side.
            if changed or time.monotonic() - self.last_input < ACTIVE_SECS:
                quiet = 0
            else:
                quiet = min(quiet + 1, 20)
            self.wake.wait(spent + 0.01 * quiet)
            self.wake.clear()

    def _set_online(self, online: bool, reason: str = "") -> None:
        if online != self.online:
            self.online = online
            self.event("online", online=online, reason=reason)

    def _stats(self) -> None:
        while True:
            before = self.received
            time.sleep(1)
            with self.clients_lock:
                watching = bool(self.clients)
            if watching:
                self.event("stats", kbps=round((self.received - before) / 1024, 1),
                           rtt=round(self.rtt, 1), w=self.screen.width, h=self.screen.height)

    # files

    def _ctl(self, fn):
        with self.ctl_lock:
            if not self.ctl._sock:
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
        return {"path": path, "volumes": volumes, "folders": folders}

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

    def _restart(self) -> None:
        self.event("toast", key="restarting")
        try:
            if self.restart:
                self.restart()
            else:
                self._ctl(lambda: self.ctl.flush(self.flush_volumes))
                try:
                    self._ctl(lambda: self.ctl.exec("Run >NIL: C:Reboot", timeout=5))
                except AmigaError:
                    pass  # it went down before answering
                self.ctl.close()
        except (AmigaError, OSError) as e:
            self.event("toast", key="restart_failed", text=str(e))


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        pass  # a page that closes mid-request is no news


class _Handler(BaseHTTPRequestHandler):
    viewer: Viewer
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass  # stdout belongs to MCP, and nobody needs the noise on stderr

    def _allowed(self, query: dict) -> bool:
        host_ok = self.headers.get("Host", "") in (
            f"127.0.0.1:{self.viewer.port}", f"localhost:{self.viewer.port}")
        token = query.get("t", [""])[0]
        return host_ok and secrets.compare_digest(token, self.viewer.token)

    def _reply(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value) -> None:
        self._reply(code, json.dumps(value).encode())

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        name = url.path.lstrip("/")
        if name in STATIC:
            return self._reply(200, (WEB / name).read_bytes(), STATIC[name])
        if not self._allowed(query):
            return self._reply(403, b"Open the viewer through amigent.", "text/plain")
        if url.path == "/":
            return self._reply(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/ws":
            return self._websocket()
        if url.path == "/api/folders":
            try:
                return self._json(200, self.viewer.folders(query.get("path", [""])[0]))
            except (AmigaError, OSError) as e:
                return self._json(502, {"error": str(e)})
        self._reply(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._allowed(query) or url.path != "/api/upload":
            return self._reply(403, b"forbidden", "text/plain")
        size = int(self.headers.get("Content-Length", "0"))
        if size > MAX_UPLOAD:
            return self._json(413, {"error": "file too large"})
        data = self.rfile.read(size)
        try:
            job = self.viewer.queue_upload(query.get("folder", ["RAM:"])[0],
                                           query.get("name", ["file"])[0], data,
                                           query.get("overwrite", ["0"])[0] == "1")
        except (AmigaError, OSError) as e:
            return self._json(502, {"error": str(e)})
        self._json(409 if job.get("exists") else 200, job)

    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._reply(400, b"websocket expected", "text/plain")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", WebSocket.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ws = WebSocket(self.rfile, self.connection)
        try:
            self.viewer.join(ws)
            while (message := ws.receive()) is not None:
                if isinstance(message, str):
                    try:
                        self.viewer.submit(json.loads(message))
                    except ValueError:
                        pass
        except (OSError, EOFError):
            pass
        finally:
            self.viewer.leave(ws)
            ws.close()
            self.close_connection = True


def windows_exe(name: str) -> str:
    """A Windows program by name; WSL started from a shortcut may lack the Windows PATH."""
    if found := shutil.which(name):
        return found
    for folder in ("/mnt/c/Windows/System32", "/mnt/c/Windows/System32/WindowsPowerShell/v1.0",
                   "/mnt/c/Windows"):
        if os.path.exists(f"{folder}/{name}"):
            return f"{folder}/{name}"
    return name


def open_window(url: str) -> None:
    """Show url as its own window (Edge or Chrome app mode), else in the default browser."""
    app = [f"--app={url}", "--window-size=1440,900", "--new-window"]
    wsl = "microsoft" in platform.release().lower()
    commands = []
    if wsl or sys.platform == "win32":
        cmd = windows_exe("cmd.exe") if wsl else "cmd"
        commands.append([cmd, "/c", "start", "", "msedge", *app])
    elif sys.platform == "darwin":
        for browser in ("Google Chrome", "Microsoft Edge", "Chromium"):
            if Path(f"/Applications/{browser}.app").exists():
                commands.append(["open", "-na", browser, "--args", *app])
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


def running_viewer(host: str) -> str | None:
    """The address of a viewer for host that already runs on this PC, if any."""
    try:
        state = json.loads(STATE.read_text())
        if state.get("host") != host:
            return None
        with urllib.request.urlopen(state["url"], timeout=2) as reply:
            return state["url"] if reply.status == 200 else None
    except (OSError, ValueError, KeyError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="amigent-viewer", description="Watch and drive the Amiga.")
    parser.add_argument("host", nargs="?", default=os.environ.get("AMIGENT_HOST", ""))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMIGENT_VIEWER_PORT", "6810")))
    parser.add_argument("--no-window", action="store_true", help="only print the address")
    parser.add_argument("--exit-when-closed", action="store_true",
                        help="stop once the window has been closed")
    parser.add_argument("--install", action="store_true",
                        help="add 'Amiga Remote' to the Windows Start menu and desktop")
    parser.add_argument("--uninstall", action="store_true", help="remove those shortcuts again")
    args = parser.parse_args()
    if args.uninstall:
        from .desktop import uninstall
        for path in uninstall():
            print(f"removed {path}")
        return
    if not args.host:
        parser.error("give the Amiga's address or set AMIGENT_HOST")
    if args.install:
        from .desktop import install
        for path in install(args.host):
            print(f"created {path}")
        return
    if url := running_viewer(args.host):
        if not args.no_window:
            open_window(url)
        print(f"amigent viewer already running: {url}", flush=True)
        return
    viewer = Viewer(
        args.host,
        ctl_port=int(os.environ.get("AMIGENT_CTL_PORT", "6800")),
        screen_port=int(os.environ.get("AMIGENT_SCREEN_PORT", "6801")),
        flush_volumes=os.environ.get("AMIGENT_FLUSH_VOLUMES", "SYS:").split(),
        remember=True,
    )
    url = viewer.start(args.port)
    print(f"amigent viewer: {url}\nCtrl+C to stop.", flush=True)
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
