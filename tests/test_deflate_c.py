"""amigentd's own deflate and inflate, built for the PC with gcc, against zlib."""

import os
import random
import re
import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "amiga" / "amigentd.c"


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    if not shutil.which("gcc"):
        pytest.skip("no gcc")
    code = SOURCE.read_text(encoding="latin-1")
    start = code.index("/* ---- deflate (RFC 1951)")
    end = code.index("/* ---- how fast this Amiga packs")
    part = code[start:end]
    part = part.replace("struct link *l;     /* source, or mem */", "struct link *l;")
    folder = tmp_path_factory.mktemp("zharness")
    src = folder / "zharness.c"
    src.write_text((ROOT / "tests/c/zharness_prelude.h").read_text() + part +
                   (ROOT / "tests/c/zharness_main.c").read_text(), encoding="latin-1")
    exe = folder / "zharness"
    done = subprocess.run(["gcc", "-O1", "-w", "-o", str(exe), str(src)], capture_output=True, text=True)
    if done.returncode:
        pytest.fail(done.stderr[-3000:])
    return exe


def run(exe, *args, data: bytes) -> bytes:
    done = subprocess.run([str(exe), *args], input=data, capture_output=True)
    assert done.returncode == 0, (args, done.returncode)
    return done.stdout


def samples():
    rnd = random.Random(5)
    text = (ROOT / "amiga" / "amigentd.c").read_bytes()[:200_000]
    yield "empty", b""
    yield "one", b"x"
    yield "text", text
    yield "random", bytes(rnd.getrandbits(8) for _ in range(70_000))
    yield "runs", b"\0" * 100_000 + b"\xff" * 3 + b"\0" * 5000
    yield "mixed", b"".join(rnd.choice([b"Workbench ", b"\0\0\0\0", bytes([rnd.getrandbits(8)])])
                            for _ in range(40_000))
    yield "long matches", (b"abcdefghij" * 3000 + bytes(range(256))) * 3


@pytest.mark.parametrize("level", [1, 2])
def test_zlib_reads_what_amigentd_packs(harness, level):
    for name, data in samples():
        packed = run(harness, "pack", str(level), data=data)
        assert zlib.decompress(packed, -15) == data, name
        if len(data) > 1000 and name not in ("random",):
            assert len(packed) < len(data) * 0.7, name
        if name == "random":
            assert len(packed) <= len(data) + len(data) // 1000 + 64  # stored, not grown


def test_level_two_packs_at_least_as_well(harness):
    data = dict(samples())["text"]
    assert len(run(harness, "pack", "2", data=data)) <= len(run(harness, "pack", "1", data=data))


@pytest.mark.parametrize("level", [1, 2])
def test_a_tile_is_one_whole_stream(harness, level):
    tile = bytes([0x11] * 2000 + list(range(200)) * 10)
    assert zlib.decompress(run(harness, "tile", str(level), data=tile), -15) == tile


@pytest.mark.parametrize("zlevel", [0, 1, 6, 9])
def test_amigentd_unpacks_what_zlib_packs(harness, zlevel):
    for name, data in samples():
        packer = zlib.compressobj(zlevel, zlib.DEFLATED, -15)
        packed = packer.compress(data) + packer.flush()
        assert run(harness, "unpack", data=packed) == data, (name, zlevel)


def test_bad_data_is_refused(harness):
    done = subprocess.run([str(harness), "unpack"], input=b"\xff\xff\xff\xff\x00", capture_output=True)
    assert done.returncode == 3
