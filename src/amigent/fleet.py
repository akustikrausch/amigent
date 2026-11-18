"""An overview of several Amigas, each with its own remote desktop.

Usage: amigent-fleet                       open the overview
       amigent-fleet --add NAME ADDRESS     remember an Amiga
       amigent-fleet --remove NAME
       amigent-fleet --list

Every Amiga gets a Viewer in this one process, so previews and remote desktop
windows share one screen connection per Amiga. Fleet-wide commands ride a
command channel of their own, so a long one never freezes a preview.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import os
import secrets
import threading
import time
from urllib.parse import parse_qs, urlparse

from . import machines, net, roster
from .errors import AmigaError
from .amigentd import AmigentdError
from .channel import Commands
from .power import flush_volumes, reboot
from .secret import FOLDER
from .viewer import WEB, Viewer, open_window
from .webserver import QuietServer, TokenHandler

PREVIEW_EVERY = 20  # seconds between previews of one Amiga; the link has no cycles to waste
PREVIEW_WIDTH = 480
RUN_TIMEOUT = 120  # one command for the whole fleet: generous, plipboxes are no T1 lines
OUTPUT_LIMIT = 8000  # chars per Amiga, tail end only; the page is no log viewer
FLEET_STATIC = {"fleet.js": "text/javascript", "fleet.css": "text/css"}


class Fleet:
    def __init__(self, amigas: list[dict], existing=None):
        self.token = secrets.token_urlsafe(18)
        self.entries = []
        self.screen_port = int(os.environ.get("AMIGENT_SCREEN_PORT", "6801"))
        self.ctl_port = int(os.environ.get("AMIGENT_CTL_PORT", "6800"))
        self.saved = all("id" in a for a in amigas)
        self.guard = threading.RLock()
        self.direct = {}
        self.httpd = None
        self.url = ""
        self.last_page = time.monotonic()
        self.overview_until = 0.0
        self.ever_joined = False
        for i, amiga in enumerate(amigas):
            self.entries.append(self._entry(amiga, i))
        if existing is not None:
            entry = next((e for e in self.entries if e["id"] == existing.host), None)
            if entry:
                entry["viewer"] = existing
            else:
                self.direct[existing.host] = existing
            existing.manager = self

    def _entry(self, amiga, index=0):
        ident = amiga.get("id", index)  # compatibility for unsaved fleet callers
        host = amiga.get("id", amiga["name"])
        return {**amiga, "id": ident, "viewer": None, "commands": Commands(host, self.ctl_port, self.screen_port),
                "busy": threading.Lock(), "online": None, "info": {}, "preview": None,
                "seen": 0.0, "error": "", "stop": threading.Event()}

    def sync(self):
        if not self.saved:
            return
        with self.guard:
            current = machines.load()
            by_id = {e["id"]: e for e in self.entries}
            updated = []
            for a in current:
                e = by_id.pop(a["id"], None)
                if e is None:
                    e = self._entry(a)
                    if self.httpd:
                        threading.Thread(target=self._watch, args=(e,), daemon=True).start()
                elif e["host"] != a["host"]:
                    self._retire(e)
                    e = self._entry(a)
                    if self.httpd:
                        threading.Thread(target=self._watch, args=(e,), daemon=True).start()
                e.update({k: a[k] for k in ("id", "name", "host")})
                e["default"] = a.get("default", False)
                updated.append(e)
            for e in by_id.values():
                self._retire(e)
            self.entries = updated

    def _retire(self, entry):
        entry["stop"].set()
        if entry["viewer"]:
            entry["viewer"].retire()
        with entry["busy"]:
            entry["commands"].close()

    def start(self, port: int = 6820) -> str:
        if self.httpd:
            return self.url
        handler = type("FleetHandler", (_FleetHandler,), {"fleet": self})
        try:
            self.httpd = QuietServer(("127.0.0.1", port), handler)
        except OSError:
            self.httpd = QuietServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        for entry in self.entries:
            threading.Thread(target=self._watch, args=(entry,), daemon=True).start()
        return self.url

    def _watch(self, entry):
        # A greeting checks reachability without taking over the screen stream
        # or running commands on every saved machine just to populate a selector.
        while not entry["stop"].is_set():
            try:
                target = machines.address(entry["id"]) if self.saved else entry["host"]
                entry["online"] = net.greet(target, self.screen_port, 2, connect_timeout=2)[:4] == b"AMGT"
                entry["error"] = "" if entry["online"] else "Not reachable"
                if entry["online"]:
                    entry["seen"] = time.time()
            except (OSError, AmigaError, ValueError) as e:
                entry["online"] = False
                entry["error"] = str(e)
            if entry["online"] and time.monotonic() < self.overview_until:
                try:
                    self._preview(entry)
                except (OSError, AmigaError, AmigentdError, EOFError, ValueError) as e:
                    entry["error"] = str(e)
            entry["stop"].wait(PREVIEW_EVERY)

    def _viewer(self, entry):
        with self.guard:
            if entry["stop"].is_set():
                raise ValueError("Machine changed; select it again in Your Amigas.")
            if entry["viewer"] is None:
                entry["viewer"] = Viewer(entry["id"] if self.saved else entry["name"],
                                        ctl_port=self.ctl_port, screen_port=self.screen_port)
                entry["viewer"].manager = self
            return entry["viewer"]

    def _preview(self, entry):
        viewer = self._viewer(entry)
        with viewer.lock():
            if not viewer.enabled:
                return
            if viewer.clients:
                picture = viewer.screen.snapshot()
            else:
                try:
                    picture = viewer.screen.capture()
                finally:
                    viewer.screen.close()
        if picture:
            scale = min(1, PREVIEW_WIDTH / picture.width)
            small = picture.resize((round(picture.width * scale), max(1, round(picture.height * scale))))
            out = io.BytesIO()
            small.save(out, "PNG")
            entry["preview"] = out.getvalue()
        entry["info"] = dict(viewer.amiga) if viewer.clients and viewer.amiga else viewer.read_amiga()

    def status(self) -> list[dict]:
        self.sync()
        with self.guard:
            entries = list(self.entries)
        rows = []
        for e in entries:
            info = e["info"]
            conflicts = machines.conflicts(e, entries) if self.saved else []
            rows.append({"id": e["id"], "name": e["name"], "host": e["host"],
                         "default": bool(e.get("default")), "conflicts": conflicts,
                         "online": False if conflicts else e["online"],
                         "error": ("Address also assigned to " + ", ".join(conflicts)) if conflicts else e["error"],
                         "os": info.get("os", ""), "cpu": info.get("cpu", ""),
                         "free": info.get("chip_free", 0) + info.get("fast_free", 0),
                         "info": info, "seen": e["seen"], "preview": bool(e["preview"]),
                         "watching": bool(e["viewer"] and e["viewer"].clients)})
        return rows

    def entry(self, ident):
        self.sync()
        with self.guard:
            found = next((e for e in self.entries if str(e["id"]) == str(ident)), None)
        if found is None:
            raise ValueError("No such saved Amiga.")
        return found

    def manage(self, body):
        ident, action = body.get("id"), body.get("action")
        self.entry(ident)
        if action == "rename":
            machines.edit(ident, name=str(body.get("name", "")))
        elif action == "address":
            machines.edit(ident, address=str(body.get("address", "")))
        elif action == "default":
            machines.edit(ident, make_default=True)
        elif action == "remove":
            machines.forget(ident)
        else:
            raise ValueError("Unknown machine action.")
        self.sync()
        return {"machines": self.status()}

    def connect(self, target):
        # Selecting a saved name/ID and a one-off address both leave the registry
        # unchanged. Never use machines.move() for a connection request.
        target = target.strip()
        known = machines.find(target)
        if known and target.casefold() != known["host"].casefold():
            return self.open_viewer(known["id"])
        target = machines.validate_address(target)
        with self.guard:
            if target not in self.direct:
                self.direct[target] = Viewer(roster.DirectAddress(target), ctl_port=self.ctl_port, screen_port=self.screen_port)
                self.direct[target].manager = self
            return self.direct[target].start(0)

    def unused_for(self):
        viewers = [e["viewer"] for e in self.entries if e["viewer"]] + list(self.direct.values())
        self.ever_joined |= any(v.ever_joined for v in viewers)
        return min([time.monotonic() - self.last_page, *(v.unused_for() for v in viewers)])

    def _command(self, entry: dict, job) -> dict:
        """One Amiga, one job, its own channel: errors come back as text, never as a guru."""
        commands = entry["commands"]
        with entry["busy"]:
            try:
                if self.saved:
                    machines.address(entry["id"])
                return {"ok": True, **(job(commands) or {})}
            except (OSError, AmigaError, AmigentdError, EOFError) as e:
                commands.close()
                return {"ok": False, "error": str(e) or type(e).__name__}

    def run(self, command: str, ids: list[int] | None = None) -> list[dict]:
        """One AmigaDOS line on every chosen Amiga at once; the A500 doesn't hold up the 060."""
        self.sync()
        chosen = [e for e in self.entries if ids is None or e["id"] in ids]

        def one(entry) -> dict:
            def job(commands):
                result = commands.exec(command, timeout=RUN_TIMEOUT)
                return {"rc": result.rc, "output": result.output[-OUTPUT_LIMIT:]}
            return {"id": entry["id"], "name": entry["name"], **self._command(entry, job)}

        if not chosen:
            return []
        with concurrent.futures.ThreadPoolExecutor(len(chosen)) as pool:
            return list(pool.map(one, chosen))

    def restart(self, index: int) -> dict:
        """Disks flushed, then a warm restart; the preview picks it up once it's back."""
        return self._command(self.entry(index), lambda commands: reboot(commands, flush_volumes()))

    def open_viewer(self, index: int) -> str:
        entry = self.entry(index)
        if self.saved:
            machines.address(entry["id"])  # refuse unresolved duplicate addresses
        with self.guard:
            return self._viewer(entry).start(0)


class _FleetHandler(TokenHandler):
    fleet: Fleet
    extra_static = FLEET_STATIC

    @property
    def gate(self):
        return self.fleet

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if self._static(url.path.lstrip("/")):
            return
        if not self._allowed(query):
            return self._reply(403, b"Open the overview through amigent-fleet.", "text/plain")
        if url.path == "/":
            return self._reply(200, (WEB / "fleet.html").read_bytes(), "text/html; charset=utf-8")
        if url.path in ("/api/fleet", "/api/machines"):
            self.fleet.last_page = time.monotonic()
            self.fleet.ever_joined = True
            if url.path == "/api/machines":
                return self._json(200, {"machines": self.fleet.status(), "overview": self.fleet.url, "current": ""})
            self.fleet.overview_until = time.monotonic() + 2 * PREVIEW_EVERY
            return self._reply(200, json.dumps(self.fleet.status()).encode(), "application/json")
        if url.path.startswith("/api/preview/"):
            try:
                preview = self.fleet.entry(url.path.rsplit("/", 1)[1])["preview"]
            except (ValueError, IndexError):
                preview = None
            if preview:
                return self._reply(200, preview, "image/png")
            return self._reply(404, b"no preview yet", "text/plain")
        if url.path.startswith("/api/open/"):
            try:
                address = self.fleet.open_viewer(url.path.rsplit("/", 1)[1])
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
        if url.path in ("/api/machines", "/api/connect"):
            try:
                result = self.fleet.manage(body) if url.path == "/api/machines" else {"url": self.fleet.connect(str(body.get("target", "")))}
                return self._json(200, result)
            except (ValueError, OSError) as e:
                return self._json(400, {"error": str(e)})
        if url.path == "/api/run":
            command = str(body.get("command", "")).strip()
            ids = body.get("ids")
            if not command or "\n" in command or (ids is not None and not isinstance(ids, list)):
                return self._reply(400, b"one command line, please", "text/plain")
            result = self.fleet.run(command, ids)
            return self._reply(200, json.dumps(result).encode(), "application/json")
        if url.path.startswith("/api/restart/"):
            try:
                result = self.fleet.restart(url.path.rsplit("/", 1)[1])
            except (ValueError, IndexError):
                return self._reply(404, b"no such Amiga", "text/plain")
            return self._reply(200, json.dumps(result).encode(), "application/json")
        self._reply(404, b"not found", "text/plain")


def main() -> None:
    parser = argparse.ArgumentParser(prog="amigent-fleet", description="An overview of your Amigas.")
    parser.add_argument("--add", nargs=2, metavar=("NAME", "ADDRESS"))
    parser.add_argument("--remove", metavar="NAME")
    parser.add_argument("--rename", nargs=2, metavar=("NAME", "NEW_NAME"))
    parser.add_argument("--default", metavar="NAME")
    parser.add_argument("--address", nargs=2, metavar=("NAME", "ADDRESS"))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--run", metavar="COMMAND", help="run on every Amiga and compare")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AMIGENT_FLEET_PORT", "6820")))
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    amigas = machines.load()
    try:
        if args.add:
            machines.remember(*args.add)
            return
        if args.rename:
            machines.edit(args.rename[0], name=args.rename[1])
            return
        if args.address:
            machines.edit(args.address[0], address=args.address[1])
            return
        if args.default:
            machines.edit(args.default, make_default=True)
            return
        if args.remove:
            if not machines.forget(args.remove):
                parser.error(f"no Amiga called {args.remove}")
            return
    except ValueError as e:
        parser.error(str(e))
    if args.run:
        print(compare_runs(run_everywhere(args.run, amigas)))
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




def compare_runs(results: list[dict]) -> str:
    """The same command on several Amigas: which agree, which differ, and how."""
    groups: dict[tuple, list[str]] = {}
    for r in results:
        key = (r.get("rc"), r.get("output", "").strip()) if r.get("ok") else ("error", r.get("error", ""))
        groups.setdefault(key, []).append(r.get("name", "?"))
    lines = []
    if len(groups) == 1:
        lines.append(f"all {len(results)} Amigas agree")
    else:
        lines.append(f"{len(groups)} different results on {len(results)} Amigas")
    for (rc, output), names in groups.items():
        head = ", ".join(names) + (f": error {output}" if rc == "error" else f": rc {rc}")
        lines.append(head)
        if rc != "error":
            lines += ["    " + x for x in output.splitlines()[-20:]]
    return "\n".join(lines)


def run_everywhere(command: str, amigas: list[dict] | None = None, timeout: float = RUN_TIMEOUT,
                   *, execute=None) -> list[dict]:
    """One AmigaDOS line on every known Amiga at once, each on a channel of its own."""
    amigas = amigas if amigas is not None else machines.load()

    def run_command(target, command, timeout):
        machines.address(target)
        commands = Commands(target)
        try:
            return commands.exec(command, timeout=timeout)
        finally:
            try:
                commands.close()
            except OSError:
                pass

    run = execute or run_command

    def one(entry: dict) -> dict:
        try:
            result = run(entry.get("id", entry["host"]), command, timeout)
            return {"name": entry["name"], "ok": True, "rc": result.rc,
                    "output": result.output[-OUTPUT_LIMIT:]}
        except (OSError, AmigaError, AmigentdError, EOFError) as e:
            return {"name": entry["name"], "ok": False, "error": str(e) or type(e).__name__}

    if not amigas:
        return []
    with concurrent.futures.ThreadPoolExecutor(len(amigas)) as pool:
        return list(pool.map(one, amigas))


if __name__ == "__main__":
    main()
