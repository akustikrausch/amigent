"""An overview of several Amigas, each with its own remote desktop.

Usage: amigent-fleet                       open the overview
       amigent-fleet --add NAME ADDRESS     remember an Amiga
       amigent-fleet --remove NAME
       amigent-fleet --list

Every Amiga gets a Viewer in this one process, so the overview's previews and
the remote desktop windows share one connection per Amiga.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from .amigactl import AmigaError
from .amigentd import AmigentdError
from .secret import FOLDER
from .viewer import STATIC, WEB, Viewer, _Server, open_window

LIST = FOLDER / "amigas.json"
PREVIEW_EVERY = 20  # seconds between previews of one Amiga
PREVIEW_WIDTH = 480


def load_amigas() -> list[dict]:
    try:
        return json.loads(LIST.read_text())
    except (OSError, ValueError):
        return []


def save_amigas(amigas: list[dict]) -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    LIST.write_text(json.dumps(amigas, indent=1))


class Fleet:
    def __init__(self, amigas: list[dict]):
        self.token = secrets.token_urlsafe(18)
        self.entries = []
        for amiga in amigas:
            viewer = Viewer(amiga["host"])
            self.entries.append({"name": amiga["name"], "host": amiga["host"], "viewer": viewer,
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
            rows.append({"id": i, "name": e["name"], "host": e["host"], "online": e["online"],
                         "os": info.get("os", ""), "cpu": info.get("cpu", ""),
                         "free": info.get("chip_free", 0) + info.get("fast_free", 0),
                         "seen": e["seen"], "watching": bool(e["viewer"].clients)})
        return rows

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
        if name in STATIC or name == "fleet.js":
            kind = STATIC.get(name, "text/javascript")
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
        amigas = [a for a in amigas if a["name"] != name] + [{"name": name, "host": host}]
        save_amigas(amigas)
        print(f"{name} at {host} added; a key for it goes into {FOLDER / ('key-' + host)} "
              "if it differs from the common one")
        return
    if args.remove:
        save_amigas([a for a in amigas if a["name"] != args.remove])
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
