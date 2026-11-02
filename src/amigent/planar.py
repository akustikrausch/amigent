"""Planar frames: the Amiga's bitplanes as they are, turned into pixels on the PC.

amigentd sends chipset screens as bitplanes (FRAME arg 2, docs/PROTOCOL.md
"planar"): no chunky conversion on a 7 MHz box, the PC does the c2p. Planes
are kept whole, so XOR tiles and HAM rows that cross tile borders just work.
"""

from __future__ import annotations

import struct

from PIL import Image, ImageChops

HAM, HAS_PALETTE, EHB = 1, 2, 4  # header flags


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


PACKING = 3  # method bits 0-1
RAW, PACKBITS, DELTA = 0, 1, 2


def _unpack(packing: int, data: bytes, size: int) -> bytes:
    raw = data if packing == RAW else unpackbits(data, size)
    if len(raw) != size:
        raise PlanarError(f"tile has {len(raw)} bytes, expected {size}")
    return raw


def _xor(a: bytes, b: bytes) -> bytes:
    """Two equal byte strings XORed as one big number: no byte loop in Python."""
    return (int.from_bytes(a, "big") ^ int.from_bytes(b, "big")).to_bytes(len(a), "big")


def _layer(bit: int) -> list:
    """Lookup for Image.point: a set pixel (255) becomes this plane's pen bit."""
    table = [0] * 256
    table[255] = 1 << bit
    return table


_LAYERS = [_layer(p) for p in range(8)]


class Planes:
    """A screen as bitplanes plus palette, updated tile by tile."""

    def __init__(self, depth: int, width: int, height: int):
        self.depth, self.width, self.height = depth, width, height
        self.stride = (width + 7) // 8
        self.planes = [bytearray(self.stride * height) for _ in range(depth)]
        self.flags = 0
        self.palette = b""  # r, g, b per pen as sent
        self.repaint = False  # colours or mode changed: every pixel on the PC is stale

    def fits(self, depth: int, width: int, height: int) -> bool:
        return (depth, width, height) == (self.depth, self.width, self.height)

    def set_palette(self, rgb: bytes) -> bool:
        """True if the colours changed, so every pixel on the PC needs a repaint."""
        changed = rgb != self.palette
        self.palette = rgb
        return changed

    def apply(self, x: int, y: int, w: int, h: int, method: int, data: bytes) -> None:
        """One tile through the stages: unpack, then XOR onto what we hold if it's a delta.

        The method byte is a bit field: bits 0-1 the packing (0 raw, 1 PackBits,
        2 PackBits of a delta). Higher bits (row XOR, shifted reference) are
        reserved for later stages and refused until PROTOCOL.md has them.
        """
        if x % 8:
            raise PlanarError(f"tile at {x},{y} is not byte aligned")
        if method & ~PACKING:
            raise PlanarError(f"tile method {method} not known here")
        packing = method & PACKING
        size = self.depth * h * ((w + 7) // 8)
        raw = _unpack(packing, data, size)
        if packing == DELTA:
            raw = _xor(raw, self.tile(x, y, w, h))
        self._store(x, y, w, h, raw)

    def tile(self, x: int, y: int, w: int, h: int) -> bytes:
        """A tile as the wire lays it out: plane 0 first, h rows of (w+7)/8 bytes each."""
        bw, left = (w + 7) // 8, x // 8
        return b"".join(plane[r * self.stride + left:r * self.stride + left + bw]
                        for plane in self.planes for r in range(y, y + h))

    def _store(self, x: int, y: int, w: int, h: int, raw: bytes) -> None:
        bw, left = (w + 7) // 8, x // 8
        at = 0
        for plane in self.planes:
            for row in range(y, y + h):
                start = row * self.stride + left
                plane[start:start + bw] = raw[at:at + bw]
                at += bw

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
