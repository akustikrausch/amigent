"""An overview of several Amigas, each with its own remote desktop.

Usage: amigent-fleet                       open the overview
       amigent-fleet --add NAME ADDRESS     remember an Amiga
       amigent-fleet --remove NAME
       amigent-fleet --list

Every Amiga gets a Viewer in this one process, so the overview's previews and
the remote desktop windows share one screen connection per Amiga. Commands for
the whole fleet ride on a command channel of their own, so a long one never
freezes a preview.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import machines
from .amigactl import AmigaError
from .amigentd import AmigentdError
from .channel import Commands
from .power import reboot
from .secret import FOLDER
from .viewer import STATIC, WEB, Viewer, _Server, open_window

PREVIEW_EVERY = 20  # seconds between previews of one Amiga
PREVIEW_WIDTH = 480
RUN_TIMEOUT = 120  # one command for the whole fleet: generous, plipboxes are no T1 lines
OUTPUT_LIMIT = 8000  # per Amiga, the page is no log viewer
FLEET_STATIC = {"fleet.js": "text/javascript", "fleet.css": "text/css"}
FLUSH_VOLUMES = os.environ.get("AMIGENT_FLUSH_VOLUMES", "SYS:").split()


def load_amigas() -> list[dict]:
    return machines.load()


def save_amigas(amigas: list[dict]) -> None:
    machines.save(amigas)


class Fleet:
    def __init__(self, amigas: list[dict]):
        self.token = secrets.token_urlsafe(18)
        self.entries = []
        for amiga in amigas:
            viewer = Viewer(amiga["name"])  # by name, so a new address is found again
            self.entries.append({"name": amiga["name"], "host": amiga["host"], "viewer": viewer,
                                 "commands": Commands(amiga["name"]), "busy": threading.Lock(),
                                 "online": None, "info": {}, "preview": None, "seen": 0.0})
        self.httpd = None
        self.url = ""

    def start(self, port: int = 6820) -> str:
        handler = type("FleetHandler", (_FleetHandler,), {"fleet": self})
        try:
            self.httpd = _Server(("127.0.0.1", port), handler)
        except OSError:
            self.httpd = _Server(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        for entry in self.entries:
            threading.Thread(target=self._watch, args=(entry,), daemon=True).start()
        return self.url

    def _watch(self, entry: dict) -> None:
        """Keep one Amiga's status and preview fresh, at a pace a slow link can take."""
        viewer = entry["viewer"]
        while True:
            try:
                with viewer.lock():
                    picture = viewer.screen.capture()
                scale = PREVIEW_WIDTH / picture.width
                small = picture.resize((PREVIEW_WIDTH, max(1, round(picture.height * scale))))
                out = io.BytesIO()
                small.save(out, "PNG", optimize=True)
                entry["preview"] = out.getvalue()
                entry["info"] = viewer.read_amiga()
                entry["online"] = True
                entry["seen"] = time.time()
            except (OSError, AmigentdError, AmigaError, EOFError) as e:
                entry["online"] = False
                entry["error"] = str(e)
                with viewer.lock():
                    viewer.screen.close()
            time.sleep(PREVIEW_EVERY)

    def status(self) -> list[dict]:
        rows = []
        for i, e in enumerate(self.entries):
            info = e["info"]
            rows.append({"id": i, "name": e["name"], "host": machines.address(e["name"]),
                         "online": e["online"],
                         "os": info.get("os", ""), "cpu": info.get("cpu", ""),
                         "free": info.get("chip_free", 0) + info.get("fast_free", 0),
                         "info": info, "seen": e["seen"], "watching": bool(e["viewer"].clients)})
        return rows

    def _command(self, entry: dict, job) -> dict:
        """One Amiga, one job, its own channel: errors come back as text, never as a crash."""
        commands = entry["commands"]
        with entry["busy"]:
            try:
                return {"ok": True, **(job(commands) or {})}
            except (OSError, AmigaError, AmigentdError, EOFError) as e:
                commands.close()
                return {"ok": False, "error": str(e) or type(e).__name__}

    def run(self, command: str, ids: list[int] | None = None) -> list[dict]:
        """The same AmigaDOS line on every chosen Amiga at once; slow ones don't hold up fast ones."""
        chosen = [i for i in range(len(self.entries)) if ids is None or i in ids]

        def one(i: int) -> dict:
            def job(commands):
                result = commands.exec(command, timeout=RUN_TIMEOUT)
                return {"rc": result.rc, "output": result.output[-OUTPUT_LIMIT:]}
            return {"id": i, "name": self.entries[i]["name"], **self._command(self.entries[i], job)}

        if not chosen:
            return []
        with concurrent.futures.ThreadPoolExecutor(len(chosen)) as pool:
            return list(pool.map(one, chosen))

    def restart(self, index: int) -> dict:
        """Disks flushed, then a warm restart; the preview finds it again once it is back."""
        return self._command(self.entries[index], lambda commands: reboot(commands, FLUSH_VOLUMES))

    def open_viewer(self, index: int) -> str:
        viewer = self.entries[index]["viewer"]
        return viewer.start(0)


class _FleetHandler(BaseHTTPRequestHandler):
    fleet: Fleet
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _allowed(self, query: dict) -> bool:
        host_ok = self.headers.get("Host", "") in (
            f"127.0.0.1:{self.fleet.port}", f"localhost:{self.fleet.port}")
        return host_ok and secrets.compare_digest(query.get("t", [""])[0], self.fleet.token)

    def _reply(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        name = url.path.lstrip("/")
        if name in STATIC or name in FLEET_STATIC:
            kind = STATIC.get(name) or FLEET_STATIC[name]
            return self._reply(200, (WEB / name).read_bytes(), kind)
        if re.fullmatch(r"locales/[a-z]{2}\.json", name) and (WEB / name).is_file():
            return self._reply(200, (WEB / name).read_bytes(), "application/json; charset=utf-8")
        if not self._allowed(query):
            return self._reply(403, b"Open the overview through amigent-fleet.", "text/plain")
        if url.path == "/":
            return self._reply(200, (WEB / "fleet.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/fleet":
            return self._reply(200, json.dumps(self.fleet.status()).encode(), "application/json")
        if url.path.startswith("/api/preview/"):
            try:
                preview = self.fleet.entries[int(url.path.rsplit("/", 1)[1])]["preview"]
            except (ValueError, IndexError):
                preview = None
            if preview:
                return self._reply(200, preview, "image/png")
            return self._reply(404, b"no preview yet", "text/plain")
        if url.path.startswith("/api/open/"):
            try:
                address = self.fleet.open_viewer(int(url.path.rsplit("/", 1)[1]))
            except (ValueError, IndexError):
                return self._reply(404, b"no such Amiga", "text/plain")
            return self._reply(200, json.dumps({"url": address}).encode(), "application/json")
        self._reply(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not self._allowed(parse_qs(url.query)):
            return self._reply(403, b"forbidden", "text/plain")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size) or b"{}") if 0 <= size <= 65536 else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self._reply(400, b"bad request", "text/plain")
        if url.path == "/api/run":
            command = str(body.get("command", "")).strip()
            ids = body.get("ids")
            if not command or "\n" in command or (ids is not None and not isinstance(ids, list)):
                return self._reply(400, b"one command line, please", "text/plain")
            result = self.fleet.run(command, [int(i) for i in ids] if ids is not None else None)
            return self._reply(200, json.dumps(result).encode(), "application/json")
        if url.path.startswith("/api/restart/"):
            try:
                result = self.fleet.restart(int(url.path.rsplit("/", 1)[1]))
            except (ValueError, IndexError):
                return self._reply(404, b"no such Amiga", "text/plain")
            return self._reply(200, json.dumps(result).encode(), "application/json")
        self._reply(404, b"not found", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser(prog="amigent-fleet", description="An overview of your Amigas.")
    parser.add_argument("--add", nargs=2, metavar=("NAME", "ADDRESS"))
    parser.add_argument("--remove", metavar="NAME")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMIGENT_FLEET_PORT", "6820")))
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    amigas = load_amigas()
    if args.add:
        name, host = args.add
        known = machines.find(host)
        if not (known and known["name"] != name and machines.rename(known["name"], name)):
            machines.remember(name, machines.address(host))
        print(f"{name} at {machines.address(host)} added; a key for it goes into "
              f"{FOLDER / ('key-' + name)} if it differs from the common one")
        return
    if args.remove:
        if not machines.forget(args.remove):
            print(f"no Amiga called {args.remove}")
        return
    if args.list or not amigas:
        for a in amigas:
            print(f"{a['name']}\t{a['host']}")
        if not amigas:
            print("No Amigas yet: amigent-fleet --add NAME ADDRESS")
        return
    fleet = Fleet(amigas)
    url = fleet.start(args.port)
    print(f"Amigent fleet: {url}\nCtrl+C to stop.", flush=True)
    if not args.no_window:
        open_window(url)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
