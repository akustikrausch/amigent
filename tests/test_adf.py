"""Disk images read on the PC: OFS and FFS, directories, hash chains, extension blocks."""

import gzip
import struct

import pytest

from amigent import adf


class Image:
    """Builds a minimal AmigaDOS floppy image for the tests."""

    def __init__(self, ffs: bool, blocks: int = 1760):
        self.ffs = ffs
        self.data = bytearray(blocks * 512)
        self.data[:4] = b"DOS" + bytes([1 if ffs else 0])
        self.root = blocks // 2
        self.next = self.root + 1
        self._header(self.root, adf.ST_ROOT, "Test", 0)

    def _alloc(self) -> int:
        n = self.next
        self.next += 1
        return n

    def _put(self, n: int, index: int, value: int) -> None:
        struct.pack_into(">l", self.data, n * 512 + index * 4, value)

    def _get(self, n: int, index: int) -> int:
        return struct.unpack_from(">l", self.data, n * 512 + index * 4)[0]

    def _header(self, n: int, sec: int, name: str, parent: int) -> None:
        self._put(n, 0, adf.T_HEADER)
        self._put(n, 1, n)
        self._put(n, 125, parent)
        self._put(n, 127, sec)
        raw = name.encode("iso-8859-1")
        self.data[n * 512 + 432] = len(raw)
        self.data[n * 512 + 433:n * 512 + 433 + len(raw)] = raw

    def _link(self, parent: int, n: int, bucket: int) -> None:
        old = self._get(parent, 6 + bucket)
        self._put(n, 124, old)  # chain the earlier entry behind this one
        self._put(parent, 6 + bucket, n)

    def mkdir(self, parent: int, name: str, bucket: int = 3) -> int:
        n = self._alloc()
        self._header(n, adf.ST_USERDIR, name, parent)
        self._link(parent, n, bucket)
        return n

    def add(self, parent: int, name: str, body: bytes, bucket: int = 5) -> int:
        n = self._alloc()
        self._header(n, adf.ST_FILE, name, parent)
        self._put(n, 81, len(body))
        self._link(parent, n, bucket)
        per = 512 if self.ffs else 488
        chunks = [body[i:i + per] for i in range(0, len(body), per)] or []
        keys = []
        for seq, chunk in enumerate(chunks, 1):
            k = self._alloc()
            keys.append(k)
            if self.ffs:
                self.data[k * 512:k * 512 + len(chunk)] = chunk
            else:
                self._put(k, 0, adf.T_DATA)
                self._put(k, 1, n)
                self._put(k, 2, seq)
                self._put(k, 3, len(chunk))
                self.data[k * 512 + 24:k * 512 + 24 + len(chunk)] = chunk
        block, rest = n, keys
        while True:
            part, rest = rest[:72], rest[72:]
            self._put(block, 2, len(part))
            for i, k in enumerate(part):
                self._put(block, 77 - i, k)
            if not rest:
                break
            ext = self._alloc()
            self._put(ext, 0, adf.T_LIST)
            self._put(ext, 127, adf.ST_FILE)
            self._put(block, 126, ext)
            block = ext
        return n


@pytest.mark.parametrize("ffs", [False, True])
def test_files_dirs_and_chains_come_out(ffs):
    img = Image(ffs)
    s = img.mkdir(img.root, "S")
    img.add(s, "Startup-Sequence", b"Echo hello\n")
    img.add(img.root, "a.txt", b"first", bucket=7)
    img.add(img.root, "b.txt", b"second", bucket=7)  # same bucket: a hash chain
    big = bytes(range(256)) * 200                         # 51200 bytes: needs an extension block
    img.add(img.root, "big", big, bucket=9)
    files = adf.extract_all(bytes(img.data))
    assert files == {"S/Startup-Sequence": b"Echo hello\n", "a.txt": b"first",
                     "b.txt": b"second", "big": big}
    assert adf.Disk(bytes(img.data)).label == "Test"


def test_gzipped_images_work():
    img = Image(True)
    img.add(img.root, "x", b"y")
    assert adf.extract_all(gzip.compress(bytes(img.data))) == {"x": b"y"}


def test_custom_format_disks_are_refused_clearly():
    with pytest.raises(adf.AdfError, match="custom format"):
        adf.extract_all(bytes(1760 * 512))
    with pytest.raises(adf.AdfError, match="880 KB"):
        adf.extract_all(b"DOS\0" + bytes(100))


def test_ofs_files_written_without_block_headers_are_read_raw():
    img = Image(False)
    n = img.add(img.root, "raw", b"z" * 488 * 2)    # OFS: 2 data blocks with headers
    body = bytes(range(256)) * 4                      # 1024 bytes = exactly 2 raw blocks
    for i, key in enumerate((n + 1, n + 2)):
        img.data[key * 512:(key + 1) * 512] = body[i * 512:(i + 1) * 512]
    img._put(n, 81, len(body))
    assert adf.extract_all(bytes(img.data))["raw"] == body
