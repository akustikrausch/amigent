"""Files out of an Amiga disk image (.adf, gzipped .adz), on the PC.

AmigaDOS floppies use 512-byte blocks: 1760 (DD) or 3520 (HD). The root
block sits in the middle; directories keep a 72-entry hash table of header
blocks with chains, a file header lists its data blocks (more in extension
blocks), and data blocks carry a 24-byte header in OFS and none in FFS.
Disks in a custom format (most bootable games) can't be read this way.
"""

from __future__ import annotations

import gzip
import struct

BSIZE = 512
T_HEADER, T_DATA, T_LIST = 2, 8, 16
ST_ROOT, ST_USERDIR, ST_FILE = 1, 2, -3
HT_SIZE = 72


class AdfError(ValueError):
    pass


def _longs(block: bytes) -> tuple:
    return struct.unpack(">128l", block)


def _name(block: bytes) -> str:
    n = min(block[432], 30)
    return block[433:433 + n].decode("iso-8859-1")


class Disk:
    def __init__(self, image: bytes):
        if image[:2] == b"\x1f\x8b":
            image = gzip.decompress(image)
        if len(image) not in (1760 * BSIZE, 3520 * BSIZE):
            raise AdfError(f"{len(image)} bytes is not an 880 KB or 1.76 MB disk image")
        if image[:3] != b"DOS":
            raise AdfError("not an AmigaDOS disk (custom format, as many games use): "
                           "write it to a floppy instead")
        self.image = image
        self.blocks = len(image) // BSIZE
        self.ffs = bool(image[3] & 1)
        self.root = self.blocks // 2
        if _longs(self.block(self.root))[127] != ST_ROOT:
            raise AdfError("the root block is not where it should be")

    def block(self, n: int) -> bytes:
        if not 2 <= n < self.blocks:
            raise AdfError(f"block {n} is outside the disk")
        return self.image[n * BSIZE:(n + 1) * BSIZE]

    @property
    def label(self) -> str:
        return _name(self.block(self.root))

    def entries(self, dir_block: int) -> list[int]:
        """Header blocks of everything in a directory, following the hash chains."""
        out, seen = [], set()
        for key in _longs(self.block(dir_block))[6:6 + HT_SIZE]:
            while key and key not in seen:
                seen.add(key)
                out.append(key)
                key = _longs(self.block(key))[124]
        return out

    def file_data(self, header: int) -> bytes:
        b = self.block(header)
        size = _longs(b)[81]
        keys: list[int] = []
        ext, hops = header, 0
        while ext:
            blk = _longs(self.block(ext))
            count = blk[2]
            keys += [blk[77 - i] for i in range(min(count, HT_SIZE))]
            ext = blk[126]
            hops += 1
            if hops > self.blocks:
                raise AdfError("extension block loop")
        data = bytearray()
        # Some OFS disks hold files written without the data block headers (FFS style);
        # the block count tells: 512 bytes a block instead of 488.
        raw_style = self.ffs or (keys and _longs(self.block(keys[0]))[0] != T_DATA
                                 and len(keys) == (size + BSIZE - 1) // BSIZE)
        for key in keys:
            raw = self.block(key)
            if raw_style:
                data += raw
            else:
                l = _longs(raw)
                if l[0] != T_DATA:
                    raise AdfError(f"block {key} is not a data block")
                data += raw[24:24 + max(0, min(l[3], BSIZE - 24))]
        if len(data) < size:
            raise AdfError(f"file at block {header} is shorter than its size says")
        return bytes(data[:size])

    def walk(self, dir_block: int | None = None, prefix: str = "",
             seen: set[int] | None = None) -> dict[str, bytes]:
        """{path: data} of every file below a directory (the root by default)."""
        out: dict[str, bytes] = {}
        dir_block = self.root if dir_block is None else dir_block
        seen = set() if seen is None else seen
        if dir_block in seen:
            raise AdfError(f"directory at block {dir_block} contains itself")
        seen.add(dir_block)
        for key in self.entries(dir_block):
            b = self.block(key)
            l = _longs(b)
            name = _name(b)
            if not name or "/" in name or name in (".", ".."):
                continue
            path = prefix + name
            if l[127] == ST_USERDIR:
                out.update(self.walk(key, path + "/", seen))
            elif l[127] == ST_FILE:
                out[path] = self.file_data(key)
        return out


def extract_all(image: bytes) -> dict[str, bytes]:
    return Disk(image).walk()
