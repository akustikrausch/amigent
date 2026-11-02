"""A slow Amiga on a slow wire, all on localhost. No real Amiga gets hurt.

SlowLink is a TCP proxy that squeezes each direction to a byte rate and adds a
one-way delay, with little buffering, so a sender blocks as amigentd does on a
plipbox. FakeAmiga speaks the amigentd screen protocol (docs/PROTOCOL.md) with
the same tile bookkeeping as amiga/amigentd.c: a reference picture per client,
reads that go stale after input or half a second, a background reader that
only runs while no command is served, and a cost per tile for reading and
packing, so a 7 MHz box feels like one.

Input leaves marks a test can wait for: every typed key paints a glyph at the
Shell cursor, a button press a block under the pointer, a move with a button
held a dot (a paint program). Mark n always has colour mark_colour(n).
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time
from collections import deque

TILE = 64
SETTLE = 0.25  # amigentd: reads less than this after input count as stale
MAX_AGE = 0.5  # and so does anything older than this

# per-tile cost in seconds: (read the screen, pack the tile)
PROFILES = {
    "68000": (0.015, 0.005),  # 7 MHz, chipset screen, c2p by hand
    "68020": (0.004, 0.0015),  # 14 MHz A1200
    "fast": (0.0, 0.0),
}


def mark_colour(n: int) -> int:
    """Colour of mark n: never the desktop's own colours (0..3), never twice in a row."""
    return 16 + n % 200


def pack(tile: bytes) -> bytes:
    """amigentd's tile packing: runs of (length-1, colour), raw when not smaller."""
    out = bytearray()
    size = len(tile)
    i = 0
    while i < size:
        c = tile[i]
        j = i + 1
        while j < size and j - i < 256 and tile[j] == c:
            j += 1
        out += bytes((j - i - 1, c))
        if len(out) >= size:
            return tile
        i = j
    return bytes(out)


class SlowLink:
    """TCP proxy: rate in bytes per second and delay in seconds, each way."""

    def __init__(self, target_port: int, rate: float, delay: float, buffer: int = 4096):
        self.target = target_port
        self.rate, self.delay, self.buffer = rate, delay, buffer
        self.down = 0  # bytes towards the PC
        self.up = 0
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self.closed = True
        self.listener.close()

    def _accept(self) -> None:
        while not self.closed:
            try:
                pc, _ = self.listener.accept()
            except OSError:
                return
            amiga = socket.create_connection(("127.0.0.1", self.target))
            for s in (pc, amiga):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                # small kernel buffers: the wire, not localhost, decides when a sender blocks
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
            self._pipe(amiga, pc, "down")
            self._pipe(pc, amiga, "up")

    def _pipe(self, src: socket.socket, dst: socket.socket, way: str) -> None:
        chunks: deque = deque()
        cond = threading.Condition()
        state = {"queued": 0, "free_at": 0.0, "eof": False}

        def read():
            while True:
                with cond:
                    while state["queued"] >= self.buffer:
                        cond.wait()
                try:
                    data = src.recv(1460)
                except OSError:
                    data = b""
                with cond:
                    if not data:
                        state["eof"] = True
                        cond.notify_all()
                        return
                    now = time.perf_counter()
                    start = max(now, state["free_at"])
                    state["free_at"] = start + len(data) / self.rate
                    chunks.append((state["free_at"] + self.delay, data))
                    state["queued"] += len(data)
                    cond.notify_all()

        def write():
            while True:
                with cond:
                    while not chunks and not state["eof"]:
                        cond.wait()
                    if not chunks:
                        break
                    due, data = chunks[0]
                wait = due - time.perf_counter()
                if wait > 0:
                    time.sleep(wait)
                try:
                    dst.sendall(data)
                except OSError:
                    break
                with cond:
                    chunks.popleft()
                    state["queued"] -= len(data)
                    setattr(self, way, getattr(self, way) + len(data))
                    cond.notify_all()
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=read, daemon=True).start()
        threading.Thread(target=write, daemon=True).start()


class FakeAmiga:
    """amigentd protocol 3 on localhost, with a desktop that can be made busy."""

    def __init__(self, width: int = 640, height: int = 256, profile: str = "68020",
                 seed: int = 1200):
        self.w, self.h = width, height
        self.read_cost, self.pack_cost = PROFILES[profile]
        self.cols, self.rows = -(-width // TILE), -(-height // TILE)
        rnd = random.Random(seed)
        self.screen = bytearray(_desktop(width, height, rnd))
        self.rnd = rnd
        self.cur: dict = {}  # tile -> bytes as last read
        self.stamp: dict = {}  # tile -> when read
        self.ref: dict = {}  # tile -> bytes as last sent
        self.wanted: dict = {}  # tiles the background reader still owes, in order
        self.input_at = 0.0
        self.pointer = (0, 0)
        self.buttons = 0
        self.marks = 0
        self.cursor = (16, height - 40)  # Shell cursor
        self.cpu = threading.Lock()  # one 680x0: commands and the reader take turns
        self.screen_lock = threading.Lock()
        self.wake_reader = threading.Event()
        self.commands = 0
        self.frames = 0
        self.cpu_time = 0.0  # seconds the 680x0 spent reading and packing for us
        self.busy = None  # (x, y, w, h) of a region that scrolls on its own
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.client: socket.socket | None = None
        self.closed = False
        for target in (self._accept, self._reader, self._animate):
            threading.Thread(target=target, daemon=True).start()

    def close(self) -> None:
        self.closed = True
        self.listener.close()
        if self.client:
            self.client.close()

    # the screen

    def _tiles(self, x, y, w, h):
        x1 = min(x + w if w else 0x7FFF, self.w)
        y1 = min(y + h if h else 0x7FFF, self.h)
        return [(tx, ty) for ty in range(y // TILE, -(-y1 // TILE))
                for tx in range(x // TILE, -(-x1 // TILE))]

    def _size(self, t):
        tx, ty = t
        return min(TILE, self.w - tx * TILE), min(TILE, self.h - ty * TILE)

    def _read(self, tiles) -> None:
        if self.read_cost:
            time.sleep(self.read_cost * len(tiles))
            self.cpu_time += self.read_cost * len(tiles)
        now = time.perf_counter()
        with self.screen_lock:
            for t in tiles:
                tw, th = self._size(t)
                x0, y0 = t[0] * TILE, t[1] * TILE
                self.cur[t] = b"".join(bytes(self.screen[(y0 + r) * self.w + x0:(y0 + r) * self.w + x0 + tw])
                                       for r in range(th))
                self.stamp[t] = now
                self.wanted.pop(t, None)

    def _stale(self, t, now) -> bool:
        s = self.stamp.get(t)
        return s is None or s - self.input_at < SETTLE or now - s > MAX_AGE

    def paint(self, x, y, w, h, colour) -> None:
        with self.screen_lock:
            for r in range(max(0, y), min(self.h, y + h)):
                a = r * self.w
                self.screen[a + max(0, x):a + min(self.w, x + w)] = bytes([colour]) * (min(self.w, x + w) - max(0, x))

    def _mark(self, x, y, w, h) -> None:
        self.marks += 1
        self.paint(x, y, w, h, mark_colour(self.marks))

    def _animate(self) -> None:
        """A region that scrolls by itself, like a log or a demo, when busy is set."""
        line = 0
        while not self.closed:
            time.sleep(0.2)
            if not self.busy:
                continue
            x, y, w, h = self.busy
            line += 1
            with self.screen_lock:
                for r in range(y, y + h - 8):
                    a = r * self.w
                    self.screen[a + x:a + x + w] = self.screen[a + 8 * self.w + x:a + 8 * self.w + x + w]
                _text_line(self.screen, self.w, x, y + h - 8, w, self.rnd)

    def _reader(self) -> None:
        """amigentd's read-ahead at priority -1: eight tiles of one row per step."""
        while not self.closed:
            self.wake_reader.wait(0.5)
            self.wake_reader.clear()
            while self.wanted and not self.closed:
                with self.cpu:
                    if not self.wanted:
                        break
                    first = next(iter(self.wanted))
                    row = [t for t in self.wanted if t[1] == first[1]][:8]
                    self._read(row)

    # the protocol

    def _accept(self) -> None:
        while not self.closed:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.sendall(b"AMGT" + struct.pack(">HH", 3, 0))
            buf = b""
            first = True
            while not self.closed:
                while len(buf) < 10:
                    more = conn.recv(4096)
                    if not more:
                        return
                    buf += more
                cmd, buf = buf[:10], buf[10:]
                if first:
                    first = False
                    if cmd[0] == 20:  # no command channels here: the PC falls back
                        conn.sendall(b"\1")
                        conn.close()
                        return
                    if self.client:  # the newcomer wins
                        try:
                            self.client.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    self.client = conn
                if conn is not self.client:
                    return
                with self.cpu:
                    self.commands += 1
                    reply = self._command(conn, *struct.unpack(">BBHHHH", cmd))
                if reply is not None:
                    conn.sendall(reply)
        except OSError:
            pass

    def _command(self, conn, cmd, arg, x, y, w, h):
        if cmd == 1:
            return struct.pack(">HHHH", self.w, self.h, TILE, 0)
        if cmd == 2:
            self._frame(conn, arg & 1, x, y, w, h)
            return None
        if cmd in (3, 4, 5, 7, 14):
            self.input_at = time.perf_counter()
        if cmd == 3:
            self.pointer = (x, y)
            if self.buttons:
                self._mark(x - 1, y - 1, 3, 3)
        elif cmd == 4:
            pressed = arg & ~self.buttons
            self.buttons = arg & 7
            if pressed:
                self._mark(self.pointer[0] - 4, self.pointer[1] - 4, 9, 9)
        elif cmd in (5, 7):
            if cmd == 7 or arg & 1:
                cx, cy = self.cursor
                self._mark(cx, cy, 6, 8)
                cx += 8
                self.cursor = (cx, cy) if cx < self.w - 16 else (16, cy)
        elif cmd == 11:
            return struct.pack(">H", 0)
        elif cmd == 18:
            return b"\1"  # nobody owns the picture here
        elif cmd not in (6, 12, 14):
            return b"\1"
        return b"\0"

    def _frame(self, conn, force, x, y, w, h) -> None:
        self.frames += 1
        tiles = self._tiles(x, y, w, h)
        now = time.perf_counter()
        self._read([t for t in tiles if force or self._stale(t, now)])
        send = [t for t in tiles if force or self.cur[t] != self.ref.get(t)]
        out = bytearray(struct.pack(">I", len(send)))
        for t in send:
            if self.pack_cost:
                time.sleep(self.pack_cost)
                self.cpu_time += self.pack_cost
            data = pack(self.cur[t])
            self.ref[t] = self.cur[t]
            tw, th = self._size(t)
            out += struct.pack(">HHHHI", t[0] * TILE, t[1] * TILE, tw, th, len(data)) + data
            if len(out) >= 16384:
                conn.sendall(out)
                out = bytearray()
        conn.sendall(out)
        for t in tiles:
            self.wanted.setdefault(t, True)
        self.wake_reader.set()


def _text_line(screen, width, x, y, w, rnd) -> None:
    """Eight rows of something that looks like text to a run-length packer."""
    for r in range(8):
        row = bytearray(b"\x01" * w)
        if r < 7:
            at = 0
            while at < w - 8:
                word = rnd.randint(2, 9) * 6
                for i in range(at, min(w, at + word)):
                    if rnd.random() < 0.45:
                        row[i] = 0
                at += word + 6
        screen[(y + r) * width + x:(y + r) * width + x + w] = row


def _desktop(width, height, rnd) -> bytes:
    """Grey backdrop, a few windows with text: packs like a real Workbench."""
    screen = bytearray(b"\x92" * (width * height))  # RGB332 grey-blue
    for wx, wy, ww, wh in ((20, 20, 300, 140), (width // 2, 40, width // 2 - 30, height // 2),
                           (10, height - 100, width - 40, 90)):
        ww, wh = min(ww, width - wx), min(wh, height - wy)
        for r in range(wy, wy + wh):
            screen[r * width + wx:r * width + wx + ww] = b"\x01" * ww
        for r in range(wy, wy + 10):
            screen[r * width + wx:r * width + wx + ww] = b"\x03" * ww
        for line in range(wy + 14, wy + wh - 10, 10):
            _text_line(screen, width, wx + 4, line, ww - 8, rnd)
    return bytes(screen)
