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

from PIL import Image

TILE = 64
SETTLE = 0.25  # amigentd: reads less than this after input count as stale
MAX_AGE = 0.5  # and so does anything older than this

# per-tile cost in seconds: (read the screen, pack the tile)
PROFILES = {
    "68000": (0.015, 0.005),  # 7 MHz, chipset screen, c2p by hand
    "68020": (0.004, 0.0015),  # 14 MHz A1200
    "fast": (0.0, 0.0),
}
# planar: (compare a tile's planes, pack it); the real 68000 did 80 ms / 0.64 s for a screen
PLANAR_PROFILES = {
    "68000": (0.002, 0.014),
    "68020": (0.0005, 0.004),
    "fast": (0.0, 0.0),
}


def mark_colour(n: int) -> int:
    """Pen of mark n: never the desktop's own (0..3), never twice in a row, fits in 4 planes."""
    return 4 + n % 12


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
                 seed: int = 1200, planar: str = "yes", depth: int = 4):
        self.w, self.h = width, height
        self.read_cost, self.pack_cost = PROFILES[profile]
        self.compare_cost, self.planar_pack = PLANAR_PROFILES[profile]
        self.planar = planar  # "yes", "rtg" (knows planar, the screen isn't), "old" (before planar)
        self.depth = depth
        self.input_between = planar != "old"
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
        self.takeovers = 0  # screen clients that came in
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
            threading.Thread(target=self._serve, args=(_Conn(conn),), daemon=True).start()

    def _serve(self, c: "_Conn") -> None:
        try:
            c.sock.sendall(b"AMGT" + struct.pack(">HH", 3, 0))
            first = True
            while not self.closed:
                cmd = c.next_command()
                if cmd is None:
                    return
                if first:
                    first = False
                    if cmd[0] == 20:  # no command channels here: the PC falls back
                        c.sock.sendall(b"\1")
                        c.sock.close()
                        return
                    self.takeovers += 1
                    if self.client:  # the newcomer wins
                        try:
                            self.client.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    self.client = c.sock
                if c.sock is not self.client:
                    return
                with self.cpu:
                    self.commands += 1
                    reply = self._command(c, *struct.unpack(">BBHHHH", cmd))
                if reply is not None:
                    c.sock.sendall(reply)
                if c.late:  # answers to input carried out during that reply
                    c.sock.sendall(bytes(c.late))
                    c.late.clear()
        except OSError:
            pass

    def _command(self, c, cmd, arg, x, y, w, h):
        if cmd == 1:
            return struct.pack(">HHHH", self.w, self.h, TILE, 0)
        if cmd == 2:
            if arg & 2 and self.planar == "yes":
                self._planar_frame(c, arg & 1, x, y, w, h)
            else:
                if arg & 2 and self.planar == "rtg":
                    c.sock.sendall(b"\0")  # format 0: the classic frame follows
                self._frame(c, arg & 1, x, y, w, h)
            return None
        if cmd in INPUT:
            return self._input(cmd, arg, x, y)
        if cmd == 11:
            return struct.pack(">H", 0)
        if cmd == 18:
            return b"\1"  # nobody owns the picture here
        if cmd not in (6, 12):
            return b"\1"
        return b"\0"

    def _input(self, cmd, arg, x, y) -> bytes:
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
        elif cmd == 7 or (cmd == 5 and arg & 1):
            cx, cy = self.cursor
            self._mark(cx, cy, 6, 8)
            cx += 8
            self.cursor = (cx, cy) if cx < self.w - 16 else (16, cy)
        return b"\0"

    def _between_tiles(self, c) -> None:
        """amigentd since planar: input that came meanwhile is carried out now, answered later."""
        if self.input_between:
            for cmd in c.input_waiting():
                c.late += self._input(cmd[0], cmd[1], *struct.unpack(">HH", cmd[2:6]))

    def _send_tiles(self, c, out: bytearray, pieces) -> None:
        for piece in pieces:
            out += piece
            if len(out) >= 2048:
                c.sock.sendall(out)
                out.clear()
                self._between_tiles(c)
        c.sock.sendall(out)

    def _spend(self, seconds: float) -> None:
        if seconds:
            time.sleep(seconds)
            self.cpu_time += seconds

    def _frame(self, c, force, x, y, w, h) -> None:
        self.frames += 1
        tiles = self._tiles(x, y, w, h)
        now = time.perf_counter()
        self._read([t for t in tiles if force or self._stale(t, now)])
        send = [t for t in tiles if force or self.cur[t] != self.ref.get(t)]

        def pieces():
            for t in send:
                self._spend(self.pack_cost)
                data = pack(self.cur[t])
                self.ref[t] = self.cur[t]
                tw, th = self._size(t)
                yield struct.pack(">HHHHI", t[0] * TILE, t[1] * TILE, tw, th, len(data)) + data

        self._send_tiles(c, bytearray(struct.pack(">I", len(send))), pieces())
        for t in tiles:
            self.wanted.setdefault(t, True)
        self.wake_reader.set()

    def _planar_frame(self, c, force, x, y, w, h) -> None:
        """Bitplanes straight from chip RAM: compare, pack, no reading ahead."""
        self.frames += 1
        tiles = self._tiles(x, y, w, h)
        known = c.planes  # per connection, as amigentd keeps it for a client it knows
        force = force or not c.palette_sent
        self._spend(self.compare_cost * len(tiles))
        with self.screen_lock:
            now = {t: self._planes(t) for t in tiles}
        send = [t for t in tiles if force or known.get(t) != now[t]]
        px, py = self.pointer[0] // TILE, self.pointer[1] // TILE
        send.sort(key=lambda t: (max(abs(t[0] - px), abs(t[1] - py)) > 1, t[1], t[0]))
        flags = 0 if c.palette_sent and not force else 2
        head = struct.pack(">BBBBHH", 1, self.depth, flags, 0, self.w, self.h)
        if flags & 2:
            colours = 1 << self.depth
            head += struct.pack(">H", colours) + bytes(
                v for pen in range(colours) for v in (_LUT_R[pen], _LUT_G[pen], _LUT_B[pen]))
            c.palette_sent = True

        def pieces():
            for t in send:
                self._spend(self.planar_pack)
                raw = now[t]
                method, data = 1, packbits(raw)
                old = known.get(t)
                if old is not None and not force:
                    delta = packbits(bytes(a ^ b for a, b in zip(raw, old)))
                    if len(delta) < len(data):
                        method, data = 2, delta
                if method == 1 and len(data) >= len(raw):
                    method, data = 0, raw
                known[t] = raw
                tw, th = self._size(t)
                yield struct.pack(">HHHHBI", t[0] * TILE, t[1] * TILE, tw, th, method, len(data)) + data

        self._send_tiles(c, bytearray(head + struct.pack(">I", len(send))), pieces())

    def _planes(self, t) -> bytes:
        """A tile as amigentd sends it: plane 0 first, rows of (w+7)/8 bytes, top bit leftmost."""
        tw, th = self._size(t)
        x0, y0 = t[0] * TILE, t[1] * TILE
        pens = b"".join(bytes(self.screen[(y0 + r) * self.w + x0:(y0 + r) * self.w + x0 + tw]) for r in range(th))
        out = []
        for p in range(self.depth):
            bits = pens.translate(_PLANE_BIT[p])
            out.append(Image.frombytes("L", (tw, th), bits).convert("1", dither=Image.Dither.NONE).tobytes())
        return b"".join(out)


INPUT = (3, 4, 5, 7, 14)  # MOVE, BUTTON, RAWKEY, CHAR, MOVE_REL
_PLANE_BIT = [bytes(255 if v >> p & 1 else 0 for v in range(256)) for p in range(8)]
_LUT_R = bytes((v & 7) * 255 // 7 for v in range(256))
_LUT_G = bytes((v >> 3 & 7) * 255 // 7 for v in range(256))
_LUT_B = bytes((v >> 6 & 3) * 255 // 3 for v in range(256))


def mark_rgb(n: int) -> tuple:
    """Mark n as the page sees it, whichever way it came."""
    c = mark_colour(n)
    return _LUT_R[c], _LUT_G[c], _LUT_B[c]


def packbits(data: bytes) -> bytes:
    """Apple's PackBits as amigentd writes it: runs of 3+ repeat, the rest literal."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        j = i + 1
        while j < n and j - i < 128 and data[j] == data[i]:
            j += 1
        if j - i >= 3:
            out += bytes((257 - (j - i), data[i]))
            i = j
            continue
        start = i
        while i < n and i - start < 128 and not (i + 2 < n and data[i] == data[i + 1] == data[i + 2]):
            i += 1
        out.append(i - start - 1)
        out += data[start:i]
    return bytes(out)


class _Conn:
    """One connection's socket, its unread commands and what amigentd keeps for it."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.late = bytearray()
        self.planes: dict = {}
        self.palette_sent = False

    def next_command(self) -> bytes | None:
        while len(self.buf) < 10:
            more = self.sock.recv(4096)
            if not more:
                return None
            self.buf += more
        cmd = bytes(self.buf[:10])
        del self.buf[:10]
        return cmd

    def input_waiting(self) -> list:
        """Input commands at the head of what came in, without waiting for more."""
        self.sock.setblocking(False)
        try:
            while True:
                more = self.sock.recv(4096)
                if not more:
                    break
                self.buf += more
        except OSError:
            pass
        finally:
            self.sock.setblocking(True)
        found = []
        while len(self.buf) >= 10 and self.buf[0] in INPUT:
            found.append(bytes(self.buf[:10]))
            del self.buf[:10]
        return found


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
    screen = bytearray(b"\x02" * (width * height))  # pen 2, the backdrop; pens double as RGB332
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
