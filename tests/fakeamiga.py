"""A pretend Amiga for the bench and the tests: amigentd protocol 3 plus a slow wire.

FakeAmiga speaks screen client and command channels against an in-memory disk
and a PIL picture as the front screen. SlowLink sits in between and plays
plipbox: limited bytes per second, latency each way, half duplex by default.
Nothing here touches a real network beyond 127.0.0.1.
"""

from __future__ import annotations

import queue
import re
import socket
import struct
import threading
import time
import zlib

from PIL import Image, ImageChops

TILE = 64
_RUN = re.compile(rb"(.)\1*", re.S)
_RUN3 = re.compile(rb"(...)\1*", re.S)


def rgb332(picture: Image.Image) -> bytes:
    """The screen as amigentd sees it: one byte per pixel, rrr in bits 0-2."""
    r, g, b = picture.convert("RGB").split()
    r = r.point(lambda v: v >> 5)
    g = g.point(lambda v: (v >> 5) << 3)
    b = b.point(lambda v: (v >> 6) << 6)
    return ImageChops.add(ImageChops.add(r, g), b).tobytes()


def pack_runs(data: bytes, unit: int = 1) -> bytes:
    """amigentd's run length packing: count-1, value; raw when that is no shorter."""
    out = bytearray()
    for m in (_RUN if unit == 1 else _RUN3).finditer(data):
        n = (m.end() - m.start()) // unit
        while n:
            k = min(n, 256)
            out.append(k - 1)
            out += m.group(1)
            n -= k
    return bytes(out) if len(out) < len(data) else data


class FakeAmiga:
    """amigentd on 127.0.0.1: one screen client, any number of command channels."""

    def __init__(self, picture: Image.Image | None = None, op_delay: float = 0.0,
                 crc_rate: float = 0.0, exec_delay: float = 0.0):
        self.picture = picture or Image.new("RGB", (640, 256), (170, 170, 170))
        self.op_delay, self.crc_rate, self.exec_delay = op_delay, crc_rate, exec_delay
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.stamps: dict[str, str] = {}
        self.counts: dict[int, int] = {}
        self.sent: dict[tuple, bytes] = {}  # tile reference, as amigentd keeps it
        self.owner = None
        self.protocol = 4
        self.pause_end = 0.0  # time.monotonic() at the end of a screen pause
        self.lock = threading.Lock()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    # disk

    @staticmethod
    def parent(path: str) -> str:
        path = path.rstrip("/")
        if "/" in path:
            return path.rsplit("/", 1)[0]
        return path.split(":", 1)[0] + ":"

    def volume(self, path: str) -> str:
        return path.split(":", 1)[0] + ":"

    def add(self, path: str, data: bytes) -> None:
        parent = self.parent(path)
        while not parent.endswith(":"):
            self.dirs.add(parent)
            parent = self.parent(parent)
        self.files[path] = data

    def _line(self, kind, name, size, stamp="2026-11-02 10:00:00") -> str:
        return f"{kind}\t{name}\t{size}\t00000000\t{stamp}\n"

    def _exists_dir(self, path: str) -> bool:
        return path.endswith(":") or path.rstrip("/") in self.dirs

    # wire

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self.listener.close()

    def _serve(self, conn):
        conn.sendall(b"AMGT" + struct.pack(">HH", self.protocol, 0))
        rd = conn.makefile("rb")
        channel, me = False, {"client": None}
        try:
            while len(head := rd.read(10)) == 10:
                cmd, arg, x, y, w, h = struct.unpack(">BBHHHH", head)
                self.counts[cmd] = self.counts.get(cmd, 0) + 1
                if cmd == 20:
                    channel = True
                    conn.sendall(b"\0")
                elif channel:
                    conn.sendall(self._channel(rd, cmd, arg, x, y, w, h))
                else:
                    conn.sendall(self._screen(me, cmd, arg, x, y, w, h))
        except OSError:
            pass
        finally:
            conn.close()

    # command channel

    @staticmethod
    def ok(body: bytes = b"") -> bytes:
        return b"\0" + body

    @staticmethod
    def fail(text: str, status: int = 1) -> bytes:
        raw = text.encode()
        return bytes([status]) + struct.pack(">H", len(raw)) + raw

    @staticmethod
    def text(body: str) -> bytes:
        raw = body.encode("iso-8859-1")
        return b"\0" + struct.pack(">I", len(raw)) + raw

    def _channel(self, rd, cmd, arg, x, y, w, h) -> bytes:
        if cmd == 19 and self.protocol >= 4:  # x is seconds here, no path follows
            self.set_pause(x)
            return self.ok()
        path = rd.read(x).decode("iso-8859-1")
        if cmd in (22, 23, 24, 25, 26, 27, 28, 29) and self.op_delay:
            time.sleep(self.op_delay)  # a 7 MHz CPU and a floppy-grade disk say hello
        if cmd == 21:
            where = rd.read(y)
            if self.exec_delay:
                time.sleep(self.exec_delay)
            out = f"ran {path} in {where.decode() or '-'}".encode()
            return self.ok(struct.pack(">iI", 0, len(out)) + out)
        if cmd == 22:
            if path not in self.files:
                return self.fail("Objekt nicht gefunden", 1)
            data = self.files[path]
            return self.ok(struct.pack(">I", len(data)) + data)
        if cmd == 23:
            data = rd.read(w << 16 | h)
            if not self._exists_dir(self.parent(path)) or path in self.dirs:
                return self.fail("Objekt nicht gefunden")
            with self.lock:
                self.files[path] = (self.files.get(path, b"") if arg else b"") + data
                self.stamps[path] = time.strftime("%Y-%m-%d %H:%M:%S")
            return self.ok()
        if cmd in (24, 25):
            return self._dir(path, cmd == 25)
        if cmd == 26:
            if self.files.pop(path, None) is None:
                return self.fail("Objekt nicht gefunden")
            return self.ok()
        if cmd == 27:
            new = rd.read(y).decode("iso-8859-1")
            if path not in self.files:
                return self.fail("Objekt nicht gefunden")
            self.files[new] = self.files.pop(path)
            return self.ok()
        if cmd == 28:
            if path.rstrip("/") in self.dirs or path in self.files:
                return self.fail("Objekt existiert bereits")
            if not self._exists_dir(self.parent(path)):
                return self.fail("Objekt nicht gefunden")
            self.dirs.add(path.rstrip("/"))
            return self.ok()
        if cmd == 29:
            if path not in self.files:
                return self.fail("Objekt nicht gefunden")
            if self.crc_rate:
                time.sleep(len(self.files[path]) / self.crc_rate)
            return self.ok(struct.pack(">I", zlib.crc32(self.files[path])))
        if cmd == 30:
            return self.text(f"chip_free=100\nkickstart=47.115\nscreen_paused={self.pause_left()}\n")
        if cmd == 31:
            return self.text("RAM Disk:\t10\t20\t30\t512\n")
        if cmd == 6:
            return b"\0"
        return self.fail("not on a command channel")

    def _dir(self, path: str, stat: bool) -> bytes:
        clean = path.rstrip("/") if not path.endswith(":") else path
        if stat:
            if clean in self.files:
                return self.text(self._line("FILE", clean.rsplit("/", 1)[-1].split(":")[-1],
                                            len(self.files[clean]), self.stamps.get(clean, "2026-11-02 10:00:00")))
            if self._exists_dir(clean):
                return self.text(self._line("DIR", clean.rsplit("/", 1)[-1], 0))
            return self.fail("Objekt nicht gefunden")
        if not self._exists_dir(clean):
            return self.fail("Objekt nicht gefunden")
        lines = []
        for d in sorted(self.dirs):
            if self.parent(d) == clean:
                lines.append(self._line("DIR", d.rsplit("/", 1)[-1].split(":")[-1], 0))
        for f, data in sorted(self.files.items()):
            if self.parent(f) == clean:
                lines.append(self._line("FILE", f.rsplit("/", 1)[-1].split(":")[-1], len(data),
                                        self.stamps.get(f, "2026-11-02 10:00:00")))
        return self.text("".join(lines))

    # screen client

    def set_pause(self, seconds: int) -> None:
        self.pause_end = time.monotonic() + seconds if seconds else 0.0

    def pause_left(self) -> int:
        left = self.pause_end - time.monotonic()
        return -int(-left // 1) if left > 0 else 0

    def _screen(self, me, cmd, arg, x, y, w, h) -> bytes:
        width, height = self.picture.size
        if cmd in (2, 13) and self.pause_left():
            x = y = 0x7FFF  # paused: off screen, as amigentd does it
            w = h = 1
        if cmd == 1:
            return struct.pack(">HHHH", width, height, TILE, self.pause_left() if self.protocol >= 4 else 0)
        if cmd == 19 and self.protocol >= 4:
            self.set_pause(x)
            return b"\0"
        if cmd == 18:
            me["client"] = x << 16 | y
            return b"\0" if me["client"] == self.owner else b"\1"
        if cmd == 2:
            self.owner = me["client"]
            return self._frame(bool(arg & 1), x, y, w or width, h or height)
        if cmd == 13:
            return self._rgb(arg, x, y, w or width - x, h or height - y)
        return b"\0"

    def _frame(self, everything: bool, x, y, w, h) -> bytes:
        width, height = self.picture.size
        screen = rgb332(self.picture)
        tiles = []
        for ty in range(0, height, TILE):
            for tx in range(0, width, TILE):
                tw, th = min(TILE, width - tx), min(TILE, height - ty)
                if tx >= x + w or tx + tw <= x or ty >= y + h or ty + th <= y:
                    continue
                data = b"".join(screen[(ty + r) * width + tx:(ty + r) * width + tx + tw]
                                for r in range(th))
                if not everything and self.sent.get((tx, ty)) == data:
                    continue
                self.sent[(tx, ty)] = data
                packed = pack_runs(data)
                tiles.append(struct.pack(">HHHHI", tx, ty, tw, th, len(packed)) + packed)
        return struct.pack(">I", len(tiles)) + b"".join(tiles)

    def _rgb(self, arg, x, y, w, h) -> bytes:
        width, height = self.picture.size
        w, h = max(0, min(w, width - x)), max(0, min(h, height - y))
        if w * h > 512 * 512:
            return struct.pack(">HH", 0, 0) + (struct.pack(">I", 0) if arg else b"")
        raw = self.picture.convert("RGB").crop((x, y, x + w, y + h)).tobytes()
        if not arg:
            return struct.pack(">HH", w, h) + raw
        packed = pack_runs(raw, 3)
        return struct.pack(">HHI", w, h, len(packed)) + packed


class SlowLink:
    """A TCP relay that behaves like a parallel-port cable with a sense of drama.

    rate in bytes per second, latency in seconds each way. shared=True makes
    both directions take turns on the wire, as plip does.
    """

    def __init__(self, target_port: int, rate: float, latency: float, shared: bool = True,
                 window: int = 32768):
        self.target, self.rate, self.latency = target_port, rate, latency
        self.shared, self.window = shared, window
        self.bytes = {"up": 0, "down": 0}
        self._free = {"up": 0.0, "down": 0.0}
        self._clock = threading.Lock()
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def reset(self) -> None:
        self.bytes = {"up": 0, "down": 0}

    def close(self):
        self.listener.close()

    def _accept(self):
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return
            server = socket.create_connection(("127.0.0.1", self.target))
            for s in (client, server):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._pump(client, server, "up")
            self._pump(server, client, "down")

    def _slot(self, direction: str, size: int) -> float:
        """When the last byte of size bytes has crossed the wire."""
        key = "up" if self.shared else direction
        with self._clock:
            start = max(time.monotonic(), self._free[key])
            self._free[key] = start + size / self.rate
            return self._free[key]

    def _pump(self, src, dst, direction):
        pending: queue.Queue = queue.Queue()
        room = threading.Semaphore(max(1, self.window // 1024))

        def reader():
            try:
                while chunk := src.recv(1024):
                    room.acquire()
                    self.bytes[direction] += len(chunk)
                    pending.put((self._slot(direction, len(chunk)) + self.latency, chunk))
            except OSError:
                pass
            pending.put(None)

        def writer():
            while (item := pending.get()) is not None:
                due, chunk = item
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    dst.sendall(chunk)
                except OSError:
                    break
                room.release()
            for s in (dst, src):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=writer, daemon=True).start()


def desktop(width: int, height: int, seed: int = 1) -> Image.Image:
    """A Workbench-ish screen: grey backdrop, a few windows full of text, some icons."""
    import random

    from PIL import ImageDraw

    rnd = random.Random(seed)
    picture = Image.new("RGB", (width, height), (149, 149, 149))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((0, 0, width, 10), fill=(255, 255, 255))
    draw.text((4, 0), "Workbench Screen  Chip 1.2M  Fast 860M", fill=(0, 0, 0))
    for _ in range(max(2, width * height // 200000)):
        wx, wy = rnd.randrange(0, width - 300), rnd.randrange(12, height - 120)
        ww, wh = rnd.randrange(200, min(700, width - wx)), rnd.randrange(80, min(500, height - wy))
        draw.rectangle((wx, wy, wx + ww, wy + wh), fill=(170, 170, 170), outline=(0, 0, 0))
        draw.rectangle((wx, wy, wx + ww, wy + 10), fill=(102, 136, 187))
        for line in range(wy + 14, wy + wh - 10, 10):
            words = " ".join(rnd.choice(["Disk", "Prefs", "Shell", "c", "libs", "Work",
                                         "s", "devs", "1.3", "readme"]) for _ in range(8))
            draw.text((wx + 4, line), words, fill=(0, 0, 0))
    for i in range(6):
        draw.rectangle((width - 60, 30 + i * 60, width - 20, 60 + i * 60), fill=(255, 136, 0))
    return picture
