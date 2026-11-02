"""Planar and chunky frames: tiles as amigentd packs them, turned into pixels on the PC.

amigentd sends chipset screens as bitplanes (FRAME arg 2, docs/PROTOCOL.md
"planar"): no chunky conversion on a 7 MHz box, the PC does the c2p. Graphics
cards come as RGB332 rows (format 2). Both are a Surface: rows of bytes, one
set per plane, and one tile pipeline undoes what the Amiga did, step by step:

    unpack (raw, PackBits)  ->  undo the row XOR  ->  XOR with the partner
    (the old picture, maybe scrolled by dy)       or  look up the index

"Old" means as before this frame: every tile saves what it overwrites, so a
partner in a tile already redrawn this frame still comes out as it was.
"""

from __future__ import annotations

import struct

from PIL import Image, ImageChops

HAM, HAS_PALETTE, EHB = 1, 2, 4  # planar header flags
PACKING = 3  # method bits 0-1: 0 raw, 1 PackBits, 2 PackBits of a delta
RAW, PACKBITS, DELTA = 0, 1, 2
ROW_XOR, SHIFTED, INDEXED = 4, 8, 16
KNOWN_METHODS = PACKING | ROW_XOR | SHIFTED | INDEXED


class PlanarError(ValueError):
    pass


def unpackbits(data: bytes, size: int) -> bytes:
    """PackBits: n < 128 copies n+1 bytes, n > 128 repeats the next byte 257-n times."""
    out = bytearray()
    at, end = 0, len(data)
    while at < end:
        n = data[at]
        if n < 128:
            out += data[at + 1:at + n + 2]
            at += n + 2
        elif n > 128:
            out += data[at + 1:at + 2] * (257 - n)
            at += 2
        else:
            at += 1  # 128 is never sent; skip it like Apple did
    if len(out) != size:
        raise PlanarError(f"PackBits gave {len(out)} bytes, expected {size}")
    return bytes(out)


def _unpack(packing: int, data: bytes, size: int) -> bytes:
    raw = data if packing == RAW else unpackbits(data, size)
    if len(raw) != size:
        raise PlanarError(f"tile has {len(raw)} bytes, expected {size}")
    return raw


def _xor(a: bytes, b: bytes) -> bytes:
    """Two equal byte strings XORed as one big number: no byte loop in Python."""
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _undo_row_xor(raw: bytes, row: int) -> bytes:
    """row[y] ^= row[y-1] from the top, block after block of rows (one block per plane)."""
    out = bytearray(len(raw))
    prev = None
    for at in range(0, len(raw), row):
        cur = int.from_bytes(raw[at:at + row], "big")
        if prev is not None:
            cur ^= prev
        out[at:at + row] = cur.to_bytes(row, "big")
        prev = cur
    return bytes(out)


def _undo_row_xor_planes(raw: bytes, row: int, rows: int) -> bytes:
    block = row * rows
    return b"".join(_undo_row_xor(raw[at:at + block], row) for at in range(0, len(raw), block)) if block else raw


def _index_table(colours: bytes, bits: int) -> list:
    """Byte of packed indices -> the RGB332 bytes it stands for, leftmost pixel first."""
    per = 8 // bits
    mask = (1 << bits) - 1
    pad = colours.ljust(1 << bits, b"\0")
    return [bytes(pad[b >> (8 - bits * (i + 1)) & mask] for i in range(per)) for b in range(256)]


class Surface:
    """Rows of bytes, one set per plane, and the tile pipeline that fills them."""

    def __init__(self, nplanes: int, width: int, height: int, stride: int):
        self.width, self.height, self.stride = width, height, stride
        self.planes = [bytearray(stride * height) for _ in range(nplanes)]
        self._before: dict = {}  # tile -> its bytes before this frame touched it

    def span(self, x: int, w: int) -> tuple[int, int]:
        """Byte offset in a row and bytes a row, for columns x..x+w."""
        raise NotImplementedError

    def begin_frame(self) -> None:
        self._before = {}

    def tile(self, x: int, y: int, w: int, h: int) -> bytes:
        """A tile as the wire lays it out: plane after plane, h rows each."""
        left, n = self.span(x, w)
        return b"".join(plane[r * self.stride + left:r * self.stride + left + n]
                        for plane in self.planes for r in range(y, y + h))

    def _old_row(self, p: int, row: int, x: int, w: int) -> bytes:
        """Row of plane p in columns x..x+w as it was before this frame; zeros off screen."""
        left, n = self.span(x, w)
        if not 0 <= row < self.height:
            return bytes(n)
        key = (x, row // 64 * 64)
        saved = self._before.get(key)
        if saved is not None and row - key[1] < saved[0]:
            th, data = saved
            at = (p * th + row - key[1]) * n
            return data[at:at + n]
        at = row * self.stride + left
        return bytes(self.planes[p][at:at + n])

    def _partner(self, x: int, y: int, w: int, h: int, dy: int) -> bytes:
        return b"".join(self._old_row(p, y + r + dy, x, w) for p in range(len(self.planes)) for r in range(h))

    def _store(self, x: int, y: int, w: int, h: int, raw: bytes) -> None:
        key = (x, y)
        if key not in self._before:
            self._before[key] = (h, self.tile(x, y, w, h))
        left, n = self.span(x, w)
        at = 0
        for plane in self.planes:
            for row in range(y, y + h):
                start = row * self.stride + left
                plane[start:start + n] = raw[at:at + n]
                at += n

    def apply(self, x: int, y: int, w: int, h: int, method: int, data: bytes) -> None:
        """One tile through the pipeline; method is the wire's bit field."""
        if method & ~KNOWN_METHODS or (method & PACKING) == 3:
            raise PlanarError(f"tile method {method} not known here")
        packing = method & PACKING
        dy = 0
        if method & SHIFTED:
            if packing != DELTA:
                raise PlanarError("a shifted tile without a delta")
            dy = struct.unpack(">h", data[:2])[0]
            data = data[2:]
        if method & INDEXED:
            raw = self._indexed(w, h, packing, method & ROW_XOR, data)
        else:
            _, n = self.span(x, w)
            raw = _unpack(packing, data, len(self.planes) * h * n)
            if method & ROW_XOR:
                raw = _undo_row_xor_planes(raw, n, h)
            if packing == DELTA:
                raw = _xor(raw, self._partner(x, y, w, h, dy))
        self._store(x, y, w, h, raw)

    def _indexed(self, w: int, h: int, packing: int, row_xor: int, data: bytes) -> bytes:
        raise PlanarError("indexed tiles only come in chunky frames")


class Chunky(Surface):
    """Format 0 and 2: one byte a pixel, RGB332, the classic frame's colours."""

    def __init__(self, width: int, height: int):
        super().__init__(1, width, height, width)

    def span(self, x: int, w: int) -> tuple[int, int]:
        return x, w

    def _indexed(self, w, h, packing, row_xor, data) -> bytes:
        """u8 k, k colours, then rows of 1, 2 or 4 bit indices, each row on a fresh byte."""
        if packing == DELTA:
            raise PlanarError("indexed tiles are never deltas")
        k = data[0]
        if not 1 <= k <= 16:
            raise PlanarError(f"{k} colours in an indexed tile")
        colours, data = data[1:1 + k], data[1 + k:]
        bits = 1 if k <= 2 else 2 if k <= 4 else 4
        row = (w * bits + 7) // 8
        raw = _unpack(packing, data, row * h)
        if row_xor:
            raw = _undo_row_xor(raw, row)
        table = _index_table(colours, bits)
        return b"".join(b"".join(map(table.__getitem__, raw[at:at + row]))[:w]
                        for at in range(0, len(raw), row))

    def paint(self, x: int, y: int, w: int, h: int, pixels: bytes) -> None:
        """Plain RGB332 rows, as a classic frame's tile unpacks to."""
        self._store(x, y, w, h, pixels)

    def image(self, palette: bytes) -> Image.Image:
        picture = Image.frombytes("P", (self.width, self.height), bytes(self.planes[0]))
        picture.putpalette(palette)
        return picture


def _layer(bit: int) -> list:
    """Lookup for Image.point: a set pixel (255) becomes this plane's pen bit."""
    table = [0] * 256
    table[255] = 1 << bit
    return table


_LAYERS = [_layer(p) for p in range(8)]


class Planes(Surface):
    """Format 1: a screen as bitplanes plus palette."""

    def __init__(self, depth: int, width: int, height: int):
        super().__init__(depth, width, height, (width + 7) // 8)
        self.depth = depth
        self.flags = 0
        self.palette = b""  # r, g, b per pen as sent
        self.repaint = False  # colours or mode changed: every pixel on the PC is stale

    def span(self, x: int, w: int) -> tuple[int, int]:
        if x % 8:
            raise PlanarError(f"tile at x {x} is not byte aligned")
        return x // 8, (w + 7) // 8

    def fits(self, depth: int, width: int, height: int) -> bool:
        return (depth, width, height) == (self.depth, self.width, self.height)

    def set_palette(self, rgb: bytes) -> bool:
        """True if the colours changed, so every pixel on the PC needs a repaint."""
        changed = rgb != self.palette
        self.palette = rgb
        return changed

    def _pens(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """Pen numbers of a region as an L image: the c2p, done by Pillow a plane at a time."""
        left, right = x // 8, (x + w + 7) // 8
        span = right - left
        pens = None
        for p, plane in enumerate(self.planes):
            rows = b"".join(plane[r * self.stride + left:r * self.stride + right] for r in range(y, y + h))
            layer = Image.frombytes("1", (span * 8, h), rows).convert("L").point(_LAYERS[p])
            pens = layer if pens is None else ImageChops.add(pens, layer)
        if pens is None:
            pens = Image.new("L", (span * 8, h))
        return pens.crop((x - left * 8, 0, x - left * 8 + w, h))

    def _palette768(self) -> bytes:
        pal = bytearray(self.palette[:768])
        if self.flags & EHB:  # pens 32-63: the first 32 at half brightness
            pal = pal[:96].ljust(96, b"\0")
            pal += bytes(c >> 1 for c in pal[:96])
        return bytes(pal.ljust(768, b"\0"))

    def image(self, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None) -> Image.Image:
        """A region in RGB."""
        w = self.width - x if w is None else w
        h = self.height - y if h is None else h
        if self.flags & HAM:
            return self._ham(x, y, w, h)
        pens = self._pens(x, y, w, h)
        picture = Image.frombytes("P", pens.size, pens.tobytes())
        picture.putpalette(self._palette768())
        return picture.convert("RGB")

    def _ham(self, x: int, y: int, w: int, h: int) -> Image.Image:
        """Hold-and-modify: every row from the left edge, colour 0 first."""
        shift = self.depth - 2  # HAM6: 4 bits of data, HAM8: 6
        mask, up = (1 << shift) - 1, 8 - shift
        keep = (1 << up) - 1
        pal = self._palette768()
        pens = self._pens(0, y, x + w, h).tobytes()
        out = bytearray()
        width = x + w
        for row in range(h):
            r, g, b = pal[0], pal[1], pal[2]
            line = pens[row * width:(row + 1) * width]
            for col, v in enumerate(line):
                d, k = v & mask, v >> shift
                if k == 0:
                    r, g, b = pal[3 * d], pal[3 * d + 1], pal[3 * d + 2]
                elif k == 1:
                    b = (b & keep) | d << up
                elif k == 2:
                    r = (r & keep) | d << up
                else:
                    g = (g & keep) | d << up
                if col >= x:
                    out += bytes((r, g, b))
        return Image.frombytes("RGB", (w, h), bytes(out))


def header(data: bytes) -> tuple[int, int, int, int]:
    """depth, flags, width, height from the 7 bytes after the format byte."""
    depth, flags, _, width, height = struct.unpack(">BBBHH", data)
    if not 1 <= depth <= 8:
        raise PlanarError(f"{depth} planes")
    return depth, flags, width, height
