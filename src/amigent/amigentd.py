"""Client for amigentd: screen tiles and input. See docs/PROTOCOL.md."""

from __future__ import annotations

import random
import select
import socket
import ssl
import struct
import time
from collections import deque

from PIL import Image

from . import tls
from .planar import HAM, HAS_PALETTE, Planes, PlanarError
from .planar import header as planar_header
from .secret import load_key

CMD_INFO, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING, CMD_CHAR = range(1, 8)
CMD_PUT, CMD_RUN, CMD_SETUP_END = 8, 9, 10
CMD_SCREENS, CMD_SCREEN_FRONT, CMD_FRAME_RGB, CMD_MOVE_REL, CMD_UITREE, CMD_MENUS = range(11, 17)
CMD_AUTH, CMD_CLIENT = 17, 18
RGB_AREA = 512 * 512  # FRAME_RGB answers regions of up to this many pixels
FRAME_PLANAR = 2  # FRAME arg bit: bitplanes welcome, see docs/PROTOCOL.md "planar"

RAWKEYS = {
    "space": 0x40, "backspace": 0x41, "tab": 0x42, "enter": 0x43, "return": 0x44,
    "esc": 0x45, "escape": 0x45, "delete": 0x46, "up": 0x4C, "down": 0x4D,
    "right": 0x4E, "left": 0x4F, "help": 0x5F,
    **{f"f{i}": 0x50 + i - 1 for i in range(1, 11)},
}
QUALIFIERS = {
    "shift": 0x01, "ctrl": 0x08, "alt": 0x10, "lamiga": 0x40, "ramiga": 0x80,
}
BUTTONS = {"left": 1, "right": 2, "middle": 4}

_LUT = {
    "r": bytes((v & 7) * 255 // 7 for v in range(256)),
    "g": bytes((v >> 3 & 7) * 255 // 7 for v in range(256)),
    "b": bytes((v >> 6 & 3) * 255 // 3 for v in range(256)),
}
# RGB332 as a palette: the picture stays one byte a pixel, like the chip RAM it came from
PALETTE = bytes(c for v in range(256) for c in (_LUT["r"][v], _LUT["g"][v], _LUT["b"][v]))
_BYTE = [bytes((v,)) for v in range(256)]


class AmigentdError(Exception):
    pass


class AmigentdRefused(AmigentdError):
    """amigentd answered, but said no: the link is fine, so nothing is retried."""


def _is_raw(data: bytes, size: int) -> bool:
    return len(data) == size and size != 0


def tile_fits(data: bytes, w: int, h: int) -> bool:
    """Does the tile unpack to exactly w*h pixels? Checked without unpacking."""
    size = w * h
    return _is_raw(data, size) or sum(data[0::2]) + len(data) // 2 == size


def unpack_tile(data: bytes, w: int, h: int) -> bytes:
    """A tile's RGB332 pixels, row by row; runs are blown up in one join, no byte loop."""
    size = w * h
    if not _is_raw(data, size):
        data = b"".join([_BYTE[c] * (n + 1) for n, c in zip(data[0::2], data[1::2])])
    if len(data) != size:
        raise AmigentdError(f"tile decoded to {len(data)} bytes, expected {size}")
    return data


def _tile_image(data: bytes, w: int, h: int) -> Image.Image:
    return Image.frombytes("P", (w, h), unpack_tile(data, w, h))


def decode_tile(data: bytes, w: int, h: int) -> Image.Image:
    image = _tile_image(data, w, h)
    image.putpalette(PALETTE)
    return image.convert("RGB")


def unpack_rgb(data: bytes, pixels: int) -> bytes:
    """FRAME_RGB arg 1: runs of (count-1, r, g, b), or raw RGB when that was shorter."""
    if len(data) == pixels * 3:
        return data
    out = bytearray()
    for i in range(0, len(data) - 3, 4):
        out += data[i + 1:i + 4] * (data[i] + 1)
    if len(out) != pixels * 3:
        raise AmigentdError(f"colour region decoded to {len(out)} bytes, expected {pixels * 3}")
    return bytes(out)


class Amigentd:
    def __init__(self, host: str, port: int = 6801, timeout: float = 30.0,
                 key: str | None = None):
        self.host = host
        self.key = key
        self.client_id = random.getrandbits(32)  # lets amigentd tell us if others took frames
        self.changes = deque(maxlen=4096)  # (time, x, y, w, h) of tiles received, by anyone
        self.received = 0  # frame bytes off the wire, for the KB/s on the page
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self.width = 0
        self.height = 0
        self._frame: Image.Image | None = None  # classic picture, mode P, see PALETTE
        self._planes: Planes | None = None  # planar picture, when amigentd sends bitplanes
        self._rendered: Image.Image | None = None  # the planes as RGB, until the next tile
        self.planar = False  # amigentd takes FRAME arg 2: found out on connect
        self._missing: set = set()  # tiles the picture still lacks, by origin
        self._unpainted: dict = {}  # tiles received but not yet painted: paid for on look only
        self._buttons = 0
        self._last_used = 0.0
        self.protocol = 0
        self.flags = 0
        self.listeners: list = []  # called as listener("size", (w, h)) or ("tiles", [...])

    def connect(self) -> None:
        self.close()
        try:
            self._sock = tls.connect(self.host, self.port, 5)  # TLS when a certificate is pinned
            self._sock.settimeout(self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = self._exact(8)
            if hello[:4] != b"AMGT":
                raise AmigentdError(f"unexpected greeting {hello!r}")
            self.protocol, self.flags = struct.unpack(">HH", hello[4:8])
            if self.flags & 1:  # bit 0: a key is required, bit 1: TLS on the next port
                self._authenticate()
            if self.protocol >= 2:
                self._send(CMD_CLIENT, 0, self.client_id >> 16, self.client_id & 0xFFFF)
                if self._exact(1) != b"\0":
                    self.drop_picture()  # frames went to someone else meanwhile
            size = self._probe_planar() if self.protocol >= 3 else self._info()
        except OSError as e:
            self.close()
            raise AmigentdError(f"amigentd at {self.host}:{self.port} not answering: {e}") from e
        except AmigentdError:
            self.close()
            raise
        self._apply_size(*size)

    def _authenticate(self) -> None:
        if self.key is None:
            self.key = load_key(self.host)  # read late: setup may have created it meanwhile
        if not self.key:
            raise AmigentdError("amigentd wants a key: run amigent-setup, or set AMIGENT_KEY")
        raw = self.key.encode()
        self._send(CMD_AUTH, 0, len(raw))
        assert self._sock
        self._sock.sendall(raw)
        if self._exact(1) != b"\0":
            raise AmigentdError("amigentd refused the key; AMIGENT_KEY or the key in ~/.config/amigent/ "
                                "must match S:amigent.key on the Amiga")

    def _info(self) -> tuple[int, int]:
        self._send(CMD_INFO)
        return struct.unpack(">HH", self._exact(8)[:4])

    def _probe_planar(self) -> tuple[int, int]:
        """Does amigentd speak planar? Ask for an empty planar frame, then INFO.

        An amigentd from before planar ignores bit 2 and answers a classic empty
        frame, u32 0; a newer one puts a format byte first. INFO's tile size (64)
        tells the two apart without a guess, and the size comes for free.
        """
        self._send(CMD_FRAME, FRAME_PLANAR, 0x7FFF, 0x7FFF, 1, 1)  # off screen: no tiles
        self._send(CMD_INFO)
        first = self._exact(1)
        if first == b"\1":
            self.planar = True
            self._drive(self._planar_reply(lambda tile: None))
            return struct.unpack(">HH", self._exact(8)[:4])
        head = first + self._exact(11)
        if head[9] == 64 and head[8] == 0:  # u32 0, then INFO: the old kind
            self.planar = False
            return struct.unpack(">HH", head[4:8])
        self.planar = True  # format 0, u32 0, INFO: new, but a screen planes can't show
        self._exact(1)  # INFO's last byte
        return struct.unpack(">HH", head[5:9])

    def _apply_size(self, width: int, height: int) -> None:
        if (width, height) != (self.width, self.height):
            self.drop_picture()
            self.width, self.height = width, height
            self._notify("size", (width, height))

    # the picture: tiles land in _unpainted and are painted only when someone looks

    def drop_picture(self) -> None:
        self._frame = None
        self._planes = None
        self._rendered = None
        self._missing = set()
        self._unpainted = {}

    def has_picture(self) -> bool:
        return self._frame is not None or self._planes is not None

    def _start_picture(self) -> None:
        self._frame = Image.new("P", (self.width, self.height))
        self._frame.putpalette(PALETTE)
        self._missing = self._all_tiles()
        self._unpainted = {}

    def _all_tiles(self) -> set:
        return {(x, y) for y in range(0, self.height, 64) for x in range(0, self.width, 64)}

    def missing_tiles(self) -> set:
        """Tile origins the picture lacks; all of them before the first frame."""
        return self._missing if self.has_picture() else self._all_tiles()

    def _take_tiles(self, tiles: list) -> None:
        """Tiles of the last reply: classic ones wait unpainted, planar ones are in already."""
        repaint = self._planes is not None and self._planes.repaint
        if not tiles and not repaint:
            return
        if self._planes is not None:
            self._rendered = None
            self._planes.repaint = False
        elif self._frame is None:
            self._start_picture()
        for tile in tiles:
            if self._planes is None:
                self._unpainted[tile[:2]] = tile
            self._missing.discard(tile[:2])
        now = time.monotonic()
        self.changes.extend((now, t[0], t[1], t[2], t[3]) for t in tiles)
        if self._planes is None:
            self._notify("tiles", tiles)
        else:  # listeners get regions to repaint; pixels() turns them into RGB
            rects = [(0, 0, self.width, self.height)] if repaint else [t[:4] for t in tiles]
            if self._planes.flags & HAM and not repaint:  # HAM colours run on to the right edge
                rects = [(0, y, self.width, h) for y, h in sorted({(t[1], t[3]) for t in tiles})]
            self._notify("planar", rects)

    def pixels(self, x: int, y: int, w: int, h: int) -> bytes:
        """RGB of a region of the picture, three bytes a pixel."""
        if self._planes is not None:
            return self._planes.image(x, y, w, h).tobytes()
        return self._paint().crop((x, y, x + w, y + h)).convert("RGB").tobytes()

    def _paint(self) -> Image.Image | None:
        if self._planes is not None:
            if self._rendered is None:
                self._rendered = self._planes.image()
            return self._rendered
        if self._frame is not None:
            for x, y, w, h, data in self._unpainted.values():
                self._frame.paste(_tile_image(data, w, h), (x, y))
        self._unpainted = {}
        return self._frame

    def _frame_reply(self, take, planar: bool = False):
        """Parser for one FRAME answer: yields bytes wanted, gets them sent in.

        take(tile) gets each tile the moment it is complete. Returns the tile
        count, the bytes and the seconds from header to last byte. With planar
        the format byte says which of the two follows.
        """
        if planar:
            kind = (yield 1)[0]
            if kind == 1:
                return (yield from self._planar_reply(take))
            if kind != 0:
                raise AmigentdError(f"unknown frame format {kind}")
        if self._planes is not None:
            self.drop_picture()  # planes out, chunky in: another screen came to the front
        count = struct.unpack(">I", (yield 4))[0]
        start, size = time.perf_counter(), 4
        for _ in range(count):
            x, y, w, h, n = struct.unpack(">HHHHI", (yield 12))
            data = yield n
            if not tile_fits(data, w, h):
                raise AmigentdError(f"tile at {x},{y} does not unpack to {w}x{h}")
            size += 12 + n
            take((x, y, w, h, data))
        self.received += size
        return count, size, time.perf_counter() - start

    def _planar_reply(self, take):
        """The planar frame after its format byte. Tiles go into the planes at once:
        an XOR tile needs the one before it."""
        depth, flags, width, height = planar_header((yield 7))
        start, size = time.perf_counter(), 8
        if (width, height) != (self.width, self.height):
            self._apply_size(width, height)
        if self._planes is None or not self._planes.fits(depth, width, height):
            self.drop_picture()
            self._planes = Planes(depth, width, height)
            self._missing = self._all_tiles()
        planes = self._planes
        planes.repaint = planes.repaint or flags != planes.flags
        planes.flags = flags
        if flags & HAS_PALETTE:
            count = struct.unpack(">H", (yield 2))[0]
            if planes.set_palette((yield 3 * count)):
                planes.repaint = True
            size += 2 + 3 * count
        count = struct.unpack(">I", (yield 4))[0]
        for _ in range(count):
            x, y, w, h, method, n = struct.unpack(">HHHHBI", (yield 13))
            data = yield n
            try:
                planes.apply(x, y, w, h, method, data)
            except PlanarError as e:
                raise AmigentdError(str(e)) from e
            size += 13 + n
            take((x, y, w, h, data))
        self.received += size
        return count, size, time.perf_counter() - start

    def _screens_reply(self):
        found = []
        for _ in range(struct.unpack(">H", (yield 2))[0]):
            handle, w, h, depth, flags, size = struct.unpack(">IHHHHH", (yield 14))
            found.append({"handle": handle, "width": w, "height": h, "depth": depth,
                          "front": bool(flags & 1), "workbench": bool(flags & 2),
                          "rtg": bool(flags & 4), "ham": bool(flags & 8),
                          "title": (yield size).decode("iso-8859-1")})
        return found

    def _drive(self, parser):
        """Feed a reply parser straight from the socket."""
        try:
            need = next(parser)
            while True:
                need = parser.send(self._exact(need))
        except StopIteration as done:
            return done.value

    def _notify(self, event: str, payload) -> None:
        for listener in self.listeners:
            try:
                listener(event, payload)
            except Exception:
                pass  # a broken viewer must not break the client

    def check_size(self) -> None:
        """Ask for the front screen's size again; a new size starts a fresh picture."""
        self._apply_size(*self._retry(self._info))

    def close(self) -> None:
        if self._sock:
            self._sock.close()
        self._sock = None

    def _exact(self, n: int) -> bytes:
        assert self._sock
        data = bytearray()
        while len(data) < n:
            chunk = self._sock.recv(min(n - len(data), 65536))
            if not chunk:
                raise AmigentdError("connection closed by amigentd")
            data += chunk
        return bytes(data)

    def _send(self, cmd: int, arg: int = 0, a: int = 0, b: int = 0, c: int = 0, d: int = 0) -> None:
        assert self._sock
        self._sock.sendall(struct.pack(">BBHHHH", cmd, arg, a, b, c, d))

    def _retry(self, fn):
        """Run fn, reconnecting once: the service may have dropped an idle link."""
        if not self._sock:
            self.connect()
        try:
            result = fn()
        except AmigentdRefused:
            raise
        except (OSError, AmigentdError, AssertionError):
            if self.protocol < 2 and time.monotonic() - self._last_used < 100:
                # Old amigentd: not the idle timeout, so another client took over
                # and may have received tiles our picture is missing.
                self.drop_picture()
            self.connect()
            result = fn()
        self._last_used = time.monotonic()
        return result

    def _ok(self, *args) -> None:
        def call():
            self._send(*args)
            if self._exact(1) != b"\0":
                raise AmigentdRefused(f"command {args[0]} refused")

        self._retry(call)

    def ping(self) -> None:
        self._ok(CMD_PING)

    def _once(self, fn):
        """For requests that must not be repeated: a broken link closes the socket."""
        if not self._sock:
            self.connect()
        try:
            return fn()
        except (OSError, AmigentdError):
            self.close()  # a late reply must not be taken for the next command's
            raise

    def put(self, path: str, data: bytes) -> None:
        """Store a file; needs amigentd started with SETUP."""
        raw = path.encode("iso-8859-1")

        def call():
            self._send(CMD_PUT, 0, len(raw), 0, len(data) >> 16, len(data) & 0xFFFF)
            self._sock.sendall(raw + data)
            return self._exact(1)

        if self._once(call) != b"\0":
            raise AmigentdRefused(f"could not write {path}")

    def run(self, command: str) -> int:
        """Run a command line and return its code; needs SETUP."""
        raw = command.encode("iso-8859-1")

        def call():
            self._send(CMD_RUN, 0, len(raw))
            self._sock.sendall(raw)
            return self._exact(1)[0]

        return self._once(call)

    def end_setup(self) -> None:
        self._ok(CMD_SETUP_END)

    def poll(self, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None) -> int:
        """Fetch the changed tiles of a region into the picture; returns how many.

        Listeners get the tiles as (x, y, w, h, data) with data as sent.
        """

        def call():
            region = (x, y, w or self.width, h or self.height)
            force = not self.has_picture() or bool(self._missing)
            if force:
                self.drop_picture()
                region = (0, 0, self.width, self.height)  # a new picture needs everything
            tiles: list = []
            try:
                self._send(CMD_FRAME, self.frame_arg(force), *region)
                self._drive(self._frame_reply(tiles.append, self.planar))
            except (OSError, AmigentdError):
                self.drop_picture()  # the service has moved on, ours is incomplete
                raise
            self._take_tiles(tiles)
            return len(tiles)

        return self._retry(call)

    def frame_arg(self, force: bool) -> int:
        return int(force) | (FRAME_PLANAR if self.planar else 0)

    def capture(self, x: int = 0, y: int = 0, w: int | None = None,
                h: int | None = None) -> Image.Image:
        """Return a region, transferring only tiles changed since the last call."""
        self._retry(lambda: self._apply_size(*self._info()))  # the front screen may have changed
        if not self.has_picture() or self._missing:
            self.poll()
        else:
            self.poll(x, y, w, h)  # tiles elsewhere wait in amigentd for later
        w, h = w or self.width - x, h or self.height - y
        if self._planes is not None:
            return self._planes.image(x, y, w, h)
        return self._paint().crop((x, y, x + w, y + h)).convert("RGB")

    def snapshot(self) -> Image.Image | None:
        """The picture as of the last poll, without asking the Amiga; black where tiles are due."""
        return self._paint()

    def move(self, x: int, y: int) -> None:
        self._ok(CMD_MOVE, 0, x, y)

    def button(self, name: str, down: bool) -> None:
        mask = BUTTONS[name]
        self.set_buttons(self._buttons | mask if down else self._buttons & ~mask)

    def set_buttons(self, mask: int) -> None:
        self._buttons = mask & 7
        self._ok(CMD_BUTTON, self._buttons)

    def rawkey(self, code: int, down: bool, qualifier: int = 0) -> None:
        self._ok(CMD_RAWKEY, int(down), code & 0x7F, qualifier)

    def char(self, ch: str, qualifier: int = 0) -> bool:
        """Type one character through the Amiga's keymap; False if it has no key for it."""
        if ord(ch) >= 256:
            return False
        try:
            self._ok(CMD_CHAR, 0, ord(ch), qualifier)
        except AmigentdRefused:
            return False
        return True

    def click(self, x: int, y: int, button: str = "left", double: bool = False) -> None:
        self.move(x, y)
        time.sleep(0.05)
        for _ in range(2 if double else 1):
            self.button(button, True)
            time.sleep(0.05)
            self.button(button, False)
            time.sleep(0.05)

    def drag(self, x1: int, y1: int, x2: int, y2: int, button: str = "left") -> None:
        self.move(x1, y1)
        time.sleep(0.05)
        self.button(button, True)
        time.sleep(0.05)
        self.move(x2, y2)
        time.sleep(0.05)
        self.button(button, False)

    # protocol 2: screens, true colour, relative mouse, the GUI as data

    def _need(self, version: int) -> None:
        if not self._sock:
            self._retry(lambda: None)  # connect, once more if another client cut in
        if self.protocol < version:
            raise AmigentdError(f"amigentd on the Amiga speaks protocol {self.protocol}, this needs "
                                f"{version}; install the current amigentd")

    def _text(self, cmd: int) -> list[list[str]]:
        def call():
            self._send(cmd)
            size = struct.unpack(">I", self._exact(4))[0]
            return self._exact(size).decode("iso-8859-1")

        self._need(2)
        return [line.split("\t") for line in self._retry(call).splitlines() if line]

    def screens(self) -> list[dict]:
        """All Intuition screens, front to back."""
        def call():
            self._send(CMD_SCREENS)
            return self._drive(self._screens_reply())

        self._need(2)
        return self._retry(call)

    def screen_to_front(self, handle: int) -> bool:
        self._need(2)

        def call():
            self._send(CMD_SCREEN_FRONT, 0, handle >> 16 & 0xFFFF, handle & 0xFFFF)
            return self._exact(1) == b"\0"

        return self._retry(call)

    def capture_rgb(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """A region in full colour, fetched packed in pieces of at most RGB_AREA pixels."""
        self._need(2)
        picture = Image.new("RGB", (w, h))
        piece_w = min(w, RGB_AREA)
        piece_h = max(1, RGB_AREA // piece_w)
        for top in range(0, h, piece_h):
            for left in range(0, w, piece_w):
                pw, ph = min(piece_w, w - left), min(piece_h, h - top)

                def call():
                    self._send(CMD_FRAME_RGB, 1, x + left, y + top, pw, ph)
                    gw, gh, size = struct.unpack(">HHI", self._exact(8))
                    return gw, gh, self._exact(size)

                gw, gh, data = self._retry(call)
                if gw and gh:
                    picture.paste(Image.frombytes("RGB", (gw, gh), unpack_rgb(data, gw * gh)),
                                  (left, top))
        return picture

    def move_relative(self, dx: int, dy: int) -> None:
        """Mouse movement as deltas, for programs that ignore where the pointer is put."""
        self._need(2)
        self._ok(CMD_MOVE_REL, 0, dx & 0xFFFF, dy & 0xFFFF)

    def ui_tree(self) -> list[dict]:
        """Front screen, its windows and their gadgets, with screen coordinates."""
        items = []
        for f in self._text(CMD_UITREE):
            if f[0] == "S" and len(f) >= 5:
                items.append({"kind": "screen", "handle": int(f[1], 16), "width": int(f[2]),
                              "height": int(f[3]), "title": f[4]})
            elif f[0] == "W" and len(f) >= 8:
                items.append({"kind": "window", "id": f[1], "x": int(f[2]), "y": int(f[3]),
                              "w": int(f[4]), "h": int(f[5]), "active": "a" in f[6],
                              "title": f[7]})
            elif f[0] == "G" and len(f) >= 10:
                items.append({"kind": "gadget", "window": f[1], "id": f[2], "x": int(f[3]),
                              "y": int(f[4]), "w": int(f[5]), "h": int(f[6]), "type": f[7],
                              "disabled": "d" in f[8], "selected": "s" in f[8], "label": f[9]})
        return items

    def menus(self) -> list[dict]:
        """Menu strip of the active window with the boxes where titles and items appear."""
        items = []
        for f in self._text(CMD_MENUS):
            kind = {"M": "menu", "I": "item", "U": "sub"}.get(f[0])
            if not kind:
                continue
            index = {"M": 2, "I": 3, "U": 4}[f[0]]
            if len(f) < index + (6 if kind == "menu" else 7):
                continue
            path = [int(v) for v in f[1:index]]
            x, y, w, h = (int(v) for v in f[index:index + 4])
            flags = f[index + 4]
            entry = {"kind": kind, "path": path, "x": x, "y": y, "w": w, "h": h,
                     "enabled": "e" in flags, "checked": "c" in flags}
            if kind == "menu":
                entry["text"] = f[index + 5]
            else:
                entry["key"], entry["text"] = f[index + 5], f[index + 6]
            items.append(entry)
        return items

    def changed_since(self, since: float, x: int = 0, y: int = 0, w: int | None = None,
                      h: int | None = None) -> int:
        """Tiles received after since that touch the region, whoever asked for them."""
        right, bottom = x + (w or self.width), y + (h or self.height)
        return sum(1 for t, tx, ty, tw, th in list(self.changes)
                   if t > since and tx < right and tx + tw > x and ty < bottom and ty + th > y)

    def type_text(self, text: str) -> None:
        """Type text; the keystrokes go out in batches and are answered together."""
        commands = []
        for ch in text:
            if ch == "\n":
                code = RAWKEYS["return"]
                commands += [(CMD_RAWKEY, 1, code, 0), (CMD_RAWKEY, 0, code, 0)]
            elif ord(ch) < 256:
                commands.append((CMD_CHAR, 0, ord(ch), 0))
        for start in range(0, len(commands), 64):
            batch = commands[start:start + 64]

            def call():
                self._sock.sendall(b"".join(struct.pack(">BBHHHH", c, a, x, y, 0, 0)
                                            for c, a, x, y in batch))
                self._exact(len(batch))  # characters the keymap lacks just answer 1

            self._retry(call)

    def press(self, combo: str) -> None:
        """Press a combination such as 'lamiga+n', 'return' or 'shift+f1'."""
        *mods, key = combo.lower().split("+")
        qualifier = 0
        for mod in mods:
            qualifier |= QUALIFIERS[mod]
        if key in RAWKEYS:
            self.rawkey(RAWKEYS[key], True, qualifier)
            self.rawkey(RAWKEYS[key], False, qualifier)
        else:
            self.char(key, qualifier)


class Pipeline:
    """Commands out back to back, answers read in order as they trickle in.

    amigentd serves one command after the other and answers in that order, so a
    queue of reply parsers, one per command sent, is all the bookkeeping there
    is. No round trip per command: on a plipbox that's the difference between
    a mouse and a slideshow. The socket stays non-blocking while the pipeline
    lives; close() hands it back as it was.
    """

    def __init__(self, screen: Amigentd, on_tiles=None):
        self.screen = screen
        self.sock = screen._sock
        self.on_tiles = on_tiles
        self.out = bytearray()
        self.inbuf = bytearray()
        self.waiting: deque = deque()  # [parser, bytes wanted, kind]
        self.frames = self.inputs = self.moves = 0
        self.heard = time.monotonic()
        self._tiles: list = []
        self.sock.settimeout(0.0)

    def close(self) -> None:
        if self.sock is self.screen._sock:
            self.sock.settimeout(self.screen.timeout)

    def busy(self) -> bool:
        return bool(self.waiting or self.out)

    def _ask(self, kind: str, parser, cmd: int, arg: int = 0, a: int = 0, b: int = 0, c: int = 0,
             d: int = 0) -> None:
        self.out += struct.pack(">BBHHHH", cmd, arg, a, b, c, d)
        self.waiting.append([parser, next(parser), kind])
        if kind == "frame":
            self.frames += 1
        elif kind != "other":
            self.inputs += 1
            self.moves += kind == "move"

    def command(self, cmd: int, arg: int = 0, x: int = 0, y: int = 0, on_reply=None) -> None:
        """Input and other one-byte answers; on_reply(ok, seconds) once it's back."""
        sent = time.perf_counter()

        def parse():
            ok = (yield 1) == b"\0"
            if on_reply:
                on_reply(ok, time.perf_counter() - sent)

        self._ask("move" if cmd in (CMD_MOVE, CMD_MOVE_REL) else "input", parse(), cmd, arg, x, y)

    def info(self) -> None:
        def parse():
            self.screen._apply_size(*struct.unpack(">HH", (yield 8)[:4]))

        self._ask("other", parse(), CMD_INFO)

    def screens(self, done) -> None:
        def parse():
            done((yield from self.screen._screens_reply()))

        self._ask("other", parse(), CMD_SCREENS)

    def frame(self, x: int, y: int, w: int, h: int, force: bool = False, on_done=None) -> None:
        """Ask for a region; tiles reach the picture as each one lands."""
        def parse():
            result = yield from self.screen._frame_reply(self._landed, self.screen.planar)
            if on_done:
                on_done(*result)

        self._ask("frame", parse(), CMD_FRAME, self.screen.frame_arg(force), x, y, w, h)

    def _landed(self, tile) -> None:
        self._tiles.append(tile)  # the list is swapped per round, so no bound .append

    def pump(self, timeout: float, bell=None) -> None:
        """One round: write what fits, read what came, hand out what's complete."""
        sock = self.sock
        pending = getattr(sock, "pending", None)  # TLS keeps decrypted bytes select can't see
        if self.out:
            self._write()
        if pending and pending():
            readable = [sock]
        else:
            watch = [sock] + ([bell] if bell is not None else [])
            readable, writable, _ = select.select(watch, [sock] if self.out else [], [], timeout)
            if writable:
                self._write()
        if sock in readable:
            try:
                data = sock.recv(65536)
            except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                data = None
            if data == b"":
                raise AmigentdError("connection closed by amigentd")
            if data:
                self.heard = time.monotonic()
                self.inbuf += data
                self._parse()
        if self.waiting and time.monotonic() - self.heard > self.screen.timeout:
            raise AmigentdError("amigentd stopped answering")
        if self._tiles:
            tiles, self._tiles = self._tiles, []
            if self.on_tiles:
                self.on_tiles(tiles)  # first: it may want to know which tiles are news
            self.screen._take_tiles(tiles)

    def _write(self) -> None:
        try:
            sent = self.sock.send(self.out)
        except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
            return
        del self.out[:sent]

    def _parse(self) -> None:
        buf = self.inbuf
        at = 0
        while self.waiting:
            entry = self.waiting[0]
            need = entry[1]
            if len(buf) - at < need:
                break
            chunk = bytes(buf[at:at + need])
            at += need
            try:
                entry[1] = entry[0].send(chunk)
            except StopIteration:
                self.waiting.popleft()
                kind = entry[2]
                if kind == "frame":
                    self.frames -= 1
                elif kind != "other":
                    self.inputs -= 1
                    self.moves -= kind == "move"
        del buf[:at]
