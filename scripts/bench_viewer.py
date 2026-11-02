"""Viewer benchmark on a simulated slow Amiga and slow wire. Offline, localhost only.

    python3 scripts/bench_viewer.py                    this tree, all links
    python3 scripts/bench_viewer.py --compare main     before (git ref) vs. now, as a table
    python3 scripts/bench_viewer.py --links 15K/100ms  just one

Each run starts tests/slowlink.py's FakeAmiga behind a SlowLink in a child
process, then a Viewer and a stand-in browser page in this one, and measures
what a person at the keyboard feels: time to the first picture, input to
visible result, what an idle screen costs, and the PC's CPU.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

LINKS = {  # name: (bytes per second, one-way delay, screen, profile)
    "50K/20ms": (50_000, 0.020, (640, 512), "68020"),
    "50K/100ms": (50_000, 0.100, (640, 512), "68020"),
    "15K/20ms": (15_000, 0.020, (640, 512), "68020"),
    "15K/100ms": (15_000, 0.100, (640, 512), "68020"),
    "68000 15K/100ms": (15_000, 0.100, (640, 256), "68000"),
}
TIMEOUT = 20.0


# ---------------------------------------------------------------- the Amiga side, in a child

def _amiga(conn, rate, delay, size, profile) -> None:
    from slowlink import FakeAmiga, SlowLink

    amiga = FakeAmiga(*size, profile=profile)
    link = SlowLink(amiga.port, rate, delay)
    conn.send(link.port)
    while True:
        msg = conn.recv()
        if msg[0] == "busy":
            amiga.busy = msg[1]
        elif msg[0] == "stats":
            conn.send({"down": link.down, "up": link.up, "frames": amiga.frames, "cpu": amiga.cpu_time,
                       "commands": amiga.commands, "marks": amiga.marks,
                       "cursor": amiga.cursor, "pointer": amiga.pointer})
        elif msg[0] == "quit":
            return


class Amiga:
    def __init__(self, rate, delay, size, profile):
        ctx = multiprocessing.get_context("fork")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_amiga, args=(child, rate, delay, size, profile), daemon=True)
        self.proc.start()
        self.port = self.conn.recv()
        self.lock = threading.Lock()

    def stats(self) -> dict:
        with self.lock:
            self.conn.send(("stats",))
            return self.conn.recv()

    def busy(self, region) -> None:
        with self.lock:
            self.conn.send(("busy", region))

    def close(self) -> None:
        with self.lock:
            self.conn.send(("quit",))
        self.proc.join(2)


# ---------------------------------------------------------------- a browser stand-in

class Page:
    """Speaks just enough WebSocket, keeps the tiles as sent, reads single pixels."""

    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), 5)
        self.sock.sendall(
            f"GET /ws?t={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.sock.settimeout(None)  # a viewer may stay silent for long: a browser waits too
        self.tiles: dict = {}  # (x, y) -> (w, h, data)
        self.bytes = 0
        self.first_tile = None
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _exact(self, n):
        while len(self.buf) < n:
            more = self.sock.recv(65536)
            if not more:
                raise EOFError
            self.buf += more
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _read(self):
        try:
            while True:
                b1, b2 = self._exact(2)
                n = b2 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", self._exact(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", self._exact(8))[0]
                data = self._exact(n)
                self.bytes += n + 2
                if b1 & 0x0F == 2 and data[:1] == b"\x01":
                    self._tiles(data)
        except (OSError, EOFError):
            pass
        except Exception as e:  # noqa: BLE001 - a dead reader must say so
            print("page reader died:", repr(e), file=sys.stderr)

    def _tiles(self, data):
        at = 1
        found = {}
        while at < len(data):
            x, y, w, h, size = struct.unpack(">HHHHI", data[at:at + 12])
            found[(x, y)] = (w, h, data[at + 12:at + 12 + size])
            at += 12 + size
        with self.cond:
            if self.first_tile is None:
                self.first_tile = time.perf_counter()
            self.tiles.update(found)
            self.cond.notify_all()

    def pixel(self, x, y):
        tile = self.tiles.get((x // 64 * 64, y // 64 * 64))
        if not tile:
            return None
        w, h, data = tile
        at = (y % 64) * w + x % 64
        if len(data) == w * h:
            return data[at]
        for i in range(0, len(data), 2):
            if at <= data[i]:
                return data[i + 1]
            at -= data[i] + 1
        return None

    def wait(self, check, timeout=TIMEOUT) -> float | None:
        end = time.perf_counter() + timeout
        with self.cond:
            while not check():
                left = end - time.perf_counter()
                if left <= 0:
                    return None
                self.cond.wait(left)
        return time.perf_counter()

    def send(self, op):
        data = json.dumps(op).encode()
        mask = os.urandom(4)
        head = bytes([0x81]) + (bytes([0x80 | len(data)]) if len(data) < 126
                                else struct.pack(">BH", 0x80 | 126, len(data)))
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


# ---------------------------------------------------------------- one run

def _isolate() -> None:
    """Like tests/conftest.py: no network search, no real config."""
    from amigent import machines, secret, tls

    folder = Path(tempfile.mkdtemp(prefix="amigent-bench-"))
    secret.FOLDER = folder
    tls.FOLDER = folder
    machines.scan = lambda: []


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run_one(name: str) -> dict:
    rate, delay, (w, h), profile = LINKS[name]
    amiga = Amiga(rate, delay, (w, h), profile)  # fork before any thread runs here
    from slowlink import mark_colour

    _isolate()
    from amigent.viewer import Viewer

    out: dict = {}
    cpu0, wall0 = os.times(), time.perf_counter()
    viewer = Viewer("127.0.0.1", screen_port=amiga.port, ctl_port=_free_port())
    viewer.start(0)
    t0 = time.perf_counter()
    page = Page(viewer.port, viewer.token)
    every = {(x, y) for y in range(0, h, 64) for x in range(0, w, 64)}
    done = page.wait(lambda: every <= page.tiles.keys(), 60)
    out["first_tile"] = (page.first_tile or t0 + 60) - t0
    out["full_picture"] = (done or t0 + 60) - t0

    time.sleep(1.5)
    s0, c0, b0, t1 = amiga.stats(), os.times(), page.bytes, time.perf_counter()
    time.sleep(5)
    s1, c1, b1, t2 = amiga.stats(), os.times(), page.bytes, time.perf_counter()
    span = t2 - t1
    out["idle_down_Bps"] = (s1["down"] - s0["down"]) / span
    out["idle_page_Bps"] = (b1 - b0) / span
    out["idle_up_Bps"] = (s1["up"] - s0["up"]) / span
    out["idle_frames_per_s"] = (s1["frames"] - s0["frames"]) / span
    out["idle_amiga_pct"] = 100 * (s1["cpu"] - s0["cpu"]) / span
    out["idle_cpu_pct"] = 100 * (c1.user + c1.system - c0.user - c0.system) / span

    def act(send, where) -> tuple[float, float]:
        """Input to Amiga, input to visible, in seconds; where gives the mark's pixel."""
        before = amiga.stats()
        x, y = where(before)
        start = time.perf_counter()
        send()
        while True:
            now = amiga.stats()
            if now["marks"] > before["marks"]:
                arrived = time.perf_counter()
                break
            if time.perf_counter() - start > TIMEOUT:
                return TIMEOUT, TIMEOUT
            time.sleep(0.002)
        colour = mark_colour(now["marks"])
        seen = page.wait(lambda: page.pixel(x, y) == colour)
        return arrived - start, (seen or start + TIMEOUT) - start

    cursor = lambda s: (s["cursor"][0] + 2, s["cursor"][1] + 2)  # noqa: E731

    def keys(label, n=5):
        lat = []
        for _ in range(n):
            time.sleep(0.3)
            lat.append(act(lambda: page.send({"t": "char", "c": "a", "q": 0}), cursor))
        out[label] = statistics.median(v for _, v in lat)
        out[label + "_amiga"] = statistics.median(a for a, _ in lat)
        if os.environ.get("BENCH_DEBUG"):
            print(label, [(round(a * 1000), round(v * 1000)) for a, v in lat], file=sys.stderr)

    keys("key_idle")
    busy = (w - 64 * 4 - 8, 56, 64 * 4, 64 * 2)  # a scrolling log in the top right
    amiga.busy(busy)
    time.sleep(1)
    keys("key_busy")

    clicks = []
    for i in range(5):
        px, py = 40 + i * 50, h // 2 - 40 + (i % 3) * 20
        page.send({"t": "move", "x": px, "y": py})
        time.sleep(0.4)
        clicks.append(act(lambda: (page.send({"t": "buttons", "mask": 1}),
                                   page.send({"t": "buttons", "mask": 0})),
                          lambda s, px=px, py=py: (px, py))[1])
    out["click_busy"] = statistics.median(clicks)

    # typing a word: from the first key to the last one visible
    time.sleep(0.5)
    first = amiga.stats()
    last_at = (first["cursor"][0] + 8 * 7 + 2, first["cursor"][1] + 2)
    start = time.perf_counter()
    for _ in range(8):
        page.send({"t": "char", "c": "w", "q": 0})
        time.sleep(0.08)
    colour = mark_colour(first["marks"] + 8)
    seen = page.wait(lambda: page.pixel(*last_at) == colour)
    out["word_busy"] = (seen or start + TIMEOUT) - start - 0.08 * 7  # from the last key

    # drag in a paint program: the end of the stroke shows up when?
    page.send({"t": "move", "x": 100, "y": h // 2 + 10})
    time.sleep(0.3)
    page.send({"t": "buttons", "mask": 1})
    time.sleep(0.3)
    for i in range(20):
        page.send({"t": "move", "x": 102 + i * 6, "y": h // 2 + 10 + i})
        time.sleep(0.03)
    end_xy = (102 + 19 * 6, h // 2 + 29)
    sent = time.perf_counter()
    while amiga.stats()["pointer"] != end_xy and time.perf_counter() - sent < TIMEOUT:
        time.sleep(0.002)
    colour = mark_colour(amiga.stats()["marks"])
    seen = page.wait(lambda: page.pixel(*end_xy) == colour)
    out["drag_end_busy"] = (seen or sent + TIMEOUT) - sent
    page.send({"t": "buttons", "mask": 0})
    time.sleep(0.5)

    # hold a key for a second at 30 repeats a second, then let go
    for i in range(31):
        page.send({"t": "key", "code": 0x20, "down": True, "q": 0, "repeat": i > 0})
        time.sleep(1 / 30)
    before = amiga.stats()["marks"]
    released = time.perf_counter()
    page.send({"t": "key", "code": 0x20, "down": False, "q": 0})
    last, still = released, before
    while time.perf_counter() - last < 1.5 and time.perf_counter() - released < 30:
        now = amiga.stats()["marks"]
        if now != still:
            still, last = now, time.perf_counter()
        time.sleep(0.005)
    out["repeat_overrun"] = last - released
    amiga.busy(None)

    c9, wall9 = os.times(), time.perf_counter()
    out["cpu_pct"] = 100 * (c9.user + c9.system - cpu0.user - cpu0.system) / (wall9 - wall0)
    out["ws_bytes"] = page.bytes
    amiga.close()
    return out


# ---------------------------------------------------------------- table

ROWS = [("first_tile", "first tile", "s"), ("full_picture", "full picture", "s"),
        ("key_idle", "key, idle screen", "ms"), ("key_busy", "key, busy screen", "ms"),
        ("key_busy_amiga", "  of it to the Amiga", "ms"),
        ("click_busy", "click, busy", "ms"), ("word_busy", "8-key word, last key", "ms"),
        ("drag_end_busy", "drag end, busy", "ms"), ("repeat_overrun", "key repeat overrun", "ms"),
        ("idle_down_Bps", "idle wire down", "B/s"), ("idle_page_Bps", "idle to the page", "B/s"),
        ("idle_up_Bps", "idle wire up", "B/s"),
        ("idle_frames_per_s", "idle FRAMEs", "/s"),
        ("idle_amiga_pct", "idle Amiga CPU for us", "%"), ("idle_cpu_pct", "idle PC CPU", "%"),
        ("cpu_pct", "PC CPU, whole run", "%")]


def _fmt(value, unit):
    if value is None:
        return "-"
    if unit == "ms":
        return f"{value * 1000:.0f}"
    if unit == "s":
        return f"{value:.2f}"
    return f"{value:.1f}"


def table(results: dict) -> str:
    """results: {impl: {link: metrics}}"""
    impls = list(results)
    lines = []
    for link in next(iter(results.values())):
        lines.append(f"\n{link}")
        lines.append(f"  {'':24}" + "".join(f"{i:>12}" for i in impls))
        for key, label, unit in ROWS:
            cells = "".join(f"{_fmt(results[i].get(link, {}).get(key), unit):>12}" for i in impls)
            lines.append(f"  {label + ' (' + unit + ')':24}{cells}")
    return "\n".join(lines)


def _run_tree(src: Path, links) -> dict:
    found = {}
    for link in links:
        done = subprocess.run([sys.executable, __file__, "--one", link, "--src", str(src)],
                              capture_output=True, text=True, timeout=600)
        if done.returncode:
            print(done.stderr, file=sys.stderr)
            found[link] = {}
            continue
        found[link] = json.loads(done.stdout.strip().splitlines()[-1])
        print(f"  {src}: {link} done", file=sys.stderr)
    return found


def _ref_tree(ref: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="amigent-ref-"))
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", ref, "src"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tmp)], input=archive, check=True)
    return tmp / "src"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--compare", metavar="REF", help="also run git REF and print both")
    parser.add_argument("--ref", metavar="REF", help="run only git REF")
    parser.add_argument("--links", nargs="*", default=list(LINKS), choices=list(LINKS))
    parser.add_argument("--save", metavar="FILE", help="also write the numbers as JSON")
    parser.add_argument("--load", metavar="FILE", nargs="*", default=[],
                        help="print saved numbers next to these (no run if alone with --ref)")
    parser.add_argument("--one", help=argparse.SUPPRESS)
    parser.add_argument("--src", default=str(ROOT / "src"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.one:
        sys.path.insert(0, args.src)
        print(json.dumps(run_one(args.one)))
        return
    results = {}
    for name in args.load:
        results.update(json.loads(Path(name).read_text()))
    if args.compare or args.ref:
        ref = args.compare or args.ref
        results[ref] = _run_tree(_ref_tree(ref), args.links)
    if not args.ref and not (args.load and not args.compare):
        results["now"] = _run_tree(ROOT / "src", args.links)
    if args.save:
        Path(args.save).write_text(json.dumps(results, indent=1))
    print(table(results))


if __name__ == "__main__":
    main()
