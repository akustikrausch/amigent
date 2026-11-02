"""Client for amigentd: screen tiles and input. See docs/PROTOCOL.md."""

from __future__ import annotations

import socket
import struct
import time

from PIL import Image

CMD_INFO, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING, CMD_CHAR = range(1, 8)
CMD_PUT, CMD_RUN, CMD_SETUP_END = 8, 9, 10

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


class AmigentdError(Exception):
    pass


def decode_tile(data: bytes, w: int, h: int) -> Image.Image:
    size = w * h
    if len(data) != size or size == 0:
        out = bytearray()
        for i in range(0, len(data), 2):
            out += bytes([data[i + 1]]) * (data[i] + 1)
        data = bytes(out)
    if len(data) != size:
        raise AmigentdError(f"tile decoded to {len(data)} bytes, expected {size}")
    bands = [Image.frombytes("L", (w, h), data.translate(_LUT[c])) for c in "rgb"]
    return Image.merge("RGB", bands)


class Amigentd:
    def __init__(self, host: str, port: int = 6801, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self.width = 0
        self.height = 0
        self._frame: Image.Image | None = None
        self._buttons = 0
        self._last_used = 0.0
        self.listeners: list = []  # called as listener("size", (w, h)) or ("tiles", [...])

    def connect(self) -> None:
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), 5)
            self._sock.settimeout(self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = self._exact(8)
            if hello[:4] != b"AMGT":
                raise AmigentdError(f"unexpected greeting {hello!r}")
            size = self._info()
        except OSError as e:
            self.close()
            raise AmigentdError(f"amigentd at {self.host}:{self.port} not answering: {e}") from e
        self._apply_size(*size)

    def _info(self) -> tuple[int, int]:
        self._send(CMD_INFO)
        return struct.unpack(">HH", self._exact(8)[:4])

    def _apply_size(self, width: int, height: int) -> None:
        if (width, height) != (self.width, self.height):
            self._frame = None
            self.width, self.height = width, height
            self._notify("size", (width, height))

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
        except (OSError, AmigentdError):
            if time.monotonic() - self._last_used < 100:
                # Not the idle timeout, so another client took over and may have
                # received tiles our picture is missing.
                self._frame = None
            self.connect()
            result = fn()
        self._last_used = time.monotonic()
        return result

    def _ok(self, *args) -> None:
        def call():
            self._send(*args)
            if self._exact(1) != b"\0":
                raise AmigentdError(f"command {args[0]} refused")

        self._retry(call)

    def ping(self) -> None:
        self._ok(CMD_PING)

    def put(self, path: str, data: bytes) -> None:
        """Store a file; needs amigentd started with SETUP."""
        raw = path.encode("iso-8859-1")
        self._send(CMD_PUT, 0, len(raw), 0, len(data) >> 16, len(data) & 0xFFFF)
        assert self._sock
        self._sock.sendall(raw + data)
        if self._exact(1) != b"\0":
            raise AmigentdError(f"could not write {path}")

    def run(self, command: str) -> int:
        """Run a command line and return its code; needs SETUP."""
        raw = command.encode("iso-8859-1")
        self._send(CMD_RUN, 0, len(raw))
        assert self._sock
        self._sock.sendall(raw)
        return self._exact(1)[0]

    def end_setup(self) -> None:
        self._ok(CMD_SETUP_END)

    def poll(self, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None) -> int:
        """Fetch the changed tiles of a region into the picture; returns how many.

        Listeners get the tiles as (x, y, w, h, data) with data as sent.
        """

        def call():
            region = (x, y, w or self.width, h or self.height)
            force = self._frame is None
            if force:
                self._frame = Image.new("RGB", (self.width, self.height))
                region = (0, 0, self.width, self.height)  # a new picture needs everything
            try:
                self._send(CMD_FRAME, int(force), *region)
                tiles = []
                for _ in range(struct.unpack(">I", self._exact(4))[0]):
                    tx, ty, tw, th, size = struct.unpack(">HHHHI", self._exact(12))
                    data = self._exact(size)
                    self._frame.paste(decode_tile(data, tw, th), (tx, ty))
                    tiles.append((tx, ty, tw, th, data))
            except (OSError, AmigentdError):
                self._frame = None  # the service has moved on, ours is incomplete
                raise
            if tiles:
                self._notify("tiles", tiles)
            return len(tiles)

        return self._retry(call)

    def capture(self, x: int = 0, y: int = 0, w: int | None = None,
                h: int | None = None) -> Image.Image:
        """Return a region, transferring only tiles changed since the last call."""
        self.poll()
        return self._frame.crop((x, y, x + (w or self.width - x), y + (h or self.height - y)))

    def snapshot(self) -> Image.Image | None:
        """The picture as of the last poll, without asking the Amiga."""
        return self._frame

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

    def char(self, ch: str, qualifier: int = 0) -> None:
        """Type one character through the Amiga's keymap; others than Latin-1 are skipped."""
        if ord(ch) < 256:
            self._ok(CMD_CHAR, 0, ord(ch), qualifier)

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

    def type_text(self, text: str) -> None:
        for ch in text:
            if ch == "\n":
                self.press("return")
            else:
                self.char(ch)

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
