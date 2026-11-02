"""Client for amigentd: screen tiles and input. See docs/PROTOCOL.md."""

from __future__ import annotations

import random
import socket
import struct
import time

from PIL import Image

from .secret import load_key

CMD_INFO, CMD_FRAME, CMD_MOVE, CMD_BUTTON, CMD_RAWKEY, CMD_PING, CMD_CHAR = range(1, 8)
CMD_PUT, CMD_RUN, CMD_SETUP_END = 8, 9, 10
CMD_SCREENS, CMD_SCREEN_FRONT, CMD_FRAME_RGB, CMD_MOVE_REL, CMD_UITREE, CMD_MENUS = range(11, 17)
CMD_AUTH, CMD_CLIENT = 17, 18
RGB_LIMIT = 512  # FRAME_RGB answers regions up to this many pixels on each side

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
    def __init__(self, host: str, port: int = 6801, timeout: float = 30.0,
                 key: str | None = None):
        self.host = host
        self.key = key
        self.client_id = random.getrandbits(32)  # lets amigentd tell us if others took frames
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self.width = 0
        self.height = 0
        self._frame: Image.Image | None = None
        self._buttons = 0
        self._last_used = 0.0
        self.protocol = 0
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
            self.protocol, locked = struct.unpack(">HH", hello[4:8])
            if locked & 1:
                self._authenticate()
            if self.protocol >= 2:
                self._send(CMD_CLIENT, 0, self.client_id >> 16, self.client_id & 0xFFFF)
                if self._exact(1) != b"\0":
                    self._frame = None  # frames went to someone else meanwhile
            size = self._info()
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
            raise AmigentdError("amigentd refused the key; AMIGENT_KEY or ~/.config/amigent/key "
                                "must match S:amigent.key on the Amiga")

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
            if self.protocol < 2 and time.monotonic() - self._last_used < 100:
                # Old amigentd: not the idle timeout, so another client took over
                # and may have received tiles our picture is missing.
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

    # protocol 2: screens, true colour, relative mouse, the GUI as data

    def _need(self, version: int) -> None:
        if not self._sock:
            self.connect()
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
            found = []
            for _ in range(struct.unpack(">H", self._exact(2))[0]):
                handle, w, h, depth, flags, size = struct.unpack(">IHHHHH", self._exact(14))
                found.append({"handle": handle, "width": w, "height": h, "depth": depth,
                              "front": bool(flags & 1), "workbench": bool(flags & 2),
                              "rtg": bool(flags & 4), "ham": bool(flags & 8),
                              "title": self._exact(size).decode("iso-8859-1")})
            return found

        self._need(2)
        return self._retry(call)

    def screen_to_front(self, handle: int) -> bool:
        self._need(2)

        def call():
            self._send(CMD_SCREEN_FRONT, 0, handle >> 16 & 0xFFFF, handle & 0xFFFF)
            return self._exact(1) == b"\0"

        return self._retry(call)

    def capture_rgb(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """A region in full colour, assembled from pieces the Amiga is willing to send."""
        self._need(2)
        picture = Image.new("RGB", (w, h))
        for top in range(0, h, RGB_LIMIT):
            for left in range(0, w, RGB_LIMIT):
                pw, ph = min(RGB_LIMIT, w - left), min(RGB_LIMIT, h - top)

                def call():
                    self._send(CMD_FRAME_RGB, 0, x + left, y + top, pw, ph)
                    gw, gh = struct.unpack(">HH", self._exact(4))
                    return gw, gh, self._exact(gw * gh * 3)

                gw, gh, data = self._retry(call)
                if gw and gh:
                    picture.paste(Image.frombytes("RGB", (gw, gh), data), (left, top))
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
            if len(f) < index + (4 if kind == "menu" else 6):
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
