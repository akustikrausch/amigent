"""Just enough LHA to unpack Aminet archives: -lh0- and -lh5- to -lh7-."""

from __future__ import annotations

import struct

MAX_MATCH = 256
THRESHOLD = 3
NC = 255 + MAX_MATCH + 2 - THRESHOLD
NP = 14
NT = 19


class Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0  # in bits, MSB first, like the blitter likes it

    def peek(self, n: int) -> int:
        byte = self.pos >> 3
        chunk = self.data[byte : byte + 4].ljust(4, b"\0")
        value = int.from_bytes(chunk, "big")
        return value >> (32 - n - (self.pos & 7)) & ((1 << n) - 1)

    def get(self, n: int) -> int:
        if n == 0:
            return 0
        value = self.peek(n)
        self.pos += n
        return value


def _table(lengths: list[int]) -> tuple[int, dict]:
    """Canonical Huffman codes as {(length, code): symbol}; bit-by-bit lookup, fine for one archive per setup."""
    codes = {}
    code = 0
    for length in range(1, 17):
        for symbol, bits in enumerate(lengths):
            if bits == length:
                codes[(length, code)] = symbol
                code += 1
        code <<= 1
    return max(lengths, default=0), codes


def _symbol(bits: Bits, table: tuple[int, dict]) -> int:
    longest, codes = table
    code = 0
    for length in range(1, longest + 1):
        code = code << 1 | bits.get(1)
        if (length, code) in codes:
            return codes[(length, code)]
    raise ValueError("bad huffman code")


def _read_lengths(bits: Bits, count: int, width: int, gap: int) -> tuple[list[int], int | None]:
    n = bits.get(width)
    if n == 0:
        return [0] * count, bits.get(width)
    lengths = [0] * count
    i = 0
    while i < n:
        c = bits.peek(3)
        if c == 7:
            mask = 1 << 12
            while bits.peek(16) & mask:
                mask >>= 1
                c += 1
            bits.pos += c - 3
        else:
            bits.pos += 3
        lengths[i] = c
        i += 1
        if i == gap:
            i += bits.get(2)
    return lengths, None


def _read_c(bits: Bits, pt: tuple[int, dict], pt_single: int | None) -> tuple[list[int], int | None]:
    n = bits.get(9)
    if n == 0:
        return [0] * NC, bits.get(9)
    lengths = [0] * NC
    i = 0
    while i < n:
        c = pt_single if pt_single is not None else _symbol(bits, pt)
        if c <= 2:
            skip = 1 if c == 0 else bits.get(4) + 3 if c == 1 else bits.get(9) + 20
            i += skip
        else:
            lengths[i] = c - 2
            i += 1
    return lengths, None


DICT_BITS = {b"-lh5-": 13, b"-lh6-": 15, b"-lh7-": 16}


def lh5_decode(data: bytes, size: int, dict_bits: int = 13) -> bytes:
    """-lh5- by default; -lh6- and -lh7- differ only in window and position codes."""
    np, pbit = dict_bits + 1, 4 if dict_bits <= 13 else 5
    bits = Bits(data)
    out = bytearray()
    while len(out) < size:
        block = bits.get(16)
        lengths, single = _read_lengths(bits, NT, 5, 3)
        c_lengths, c_single = _read_c(bits, _table(lengths), single)
        c_table = _table(c_lengths) if c_single is None else None
        p_lengths, p_single = _read_lengths(bits, np, pbit, -1)
        p_table = _table(p_lengths) if p_single is None else None
        for _ in range(block):
            symbol = c_single if c_table is None else _symbol(bits, c_table)
            if symbol < 256:
                out.append(symbol)
                continue
            length = symbol - 256 + THRESHOLD
            p = p_single if p_table is None else _symbol(bits, p_table)
            distance = p if p < 2 else (1 << (p - 1)) + bits.get(p - 1)
            start = len(out) - distance - 1
            for k in range(length):
                out.append(out[start + k])
    return bytes(out[:size])


def entries(archive: bytes):
    """Yield (name, method, packed body, size) per member; header levels 0, 1 and 2."""
    pos = 0
    while pos < len(archive) and archive[pos]:
        level = archive[pos + 20]
        method = archive[pos + 2 : pos + 7]
        packed, size = struct.unpack_from("<II", archive, pos + 7)
        if level == 0 or level == 1:
            head = archive[pos] + 2
            n = archive[pos + 21]
            name = archive[pos + 22 : pos + 22 + n]
            if level == 1:
                ext = struct.unpack_from("<H", archive, pos + head - 2)[0]
                off = pos + head
                while ext:
                    if archive[off] == 2:
                        name = archive[off + 1 : off + ext - 2].replace(b"\xff", b"/") + b"/" + name
                    packed -= ext
                    head += ext
                    next_ext = struct.unpack_from("<H", archive, off + ext - 2)[0]
                    off += ext
                    ext = next_ext
        elif level == 2:
            head = struct.unpack_from("<H", archive, pos)[0]
            off = pos + 24
            name = b""
            directory = b""
            ext = struct.unpack_from("<H", archive, off)[0]
            off += 2
            while ext:
                kind = archive[off]
                if kind == 1:
                    name = archive[off + 1 : off + ext - 2]
                elif kind == 2:
                    directory = archive[off + 1 : off + ext - 2].replace(b"\xff", b"/")
                next_ext = struct.unpack_from("<H", archive, off + ext - 2)[0]
                off += ext
                ext = next_ext
            name = directory + name
        else:
            raise ValueError(f"LHA header level {level}")
        body = archive[pos + head : pos + head + packed]
        yield name.decode("latin-1"), method, body, size
        pos += head + packed


def unpack(method: bytes, body: bytes, size: int) -> bytes:
    if method in (b"-lh0-", b"-lhd-"):
        return body
    if method in DICT_BITS:
        return lh5_decode(body, size, DICT_BITS[method])
    raise ValueError(f"cannot unpack {method!r}")


def extract(archive: bytes, suffix: str) -> bytes:
    """The first member whose name ends with suffix, unpacked."""
    for name, method, body, size in entries(archive):
        if name.lower().endswith(suffix.lower()):
            return unpack(method, body, size)
    raise KeyError(suffix)


def extract_all(archive: bytes) -> dict[str, bytes]:
    """Every file member by its path in the archive; directories (-lhd-) left out."""
    return {name: unpack(method, body, size) for name, method, body, size in entries(archive)
            if method != b"-lhd-" and not name.endswith("/")}
