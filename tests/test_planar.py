import struct
import time

import pytest

from amigent.amigentd import Amigentd
from amigent.planar import EHB, HAM, Planes, PlanarError, unpackbits
from slowlink import FakeAmiga, mark_rgb, packbits


def test_packbits_round_trip():
    for data in (b"", b"a", b"aaaa", bytes(range(200)), b"ab" * 100 + b"\0" * 300, bytes(1000)):
        assert unpackbits(packbits(data), len(data)) == data
    assert unpackbits(bytes([0xFE, 7, 1, 1, 2]), 5) == b"\7\7\7\1\2"
    with pytest.raises(PlanarError):
        unpackbits(bytes([0xFE, 7]), 4)


def _tile(planes):
    """A 16x1 tile from one row per plane, as the wire has it."""
    return b"".join(planes)


def test_planes_turn_into_pens_and_colours():
    p = Planes(2, 16, 1)
    p.set_palette(bytes([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255]))
    p.apply(0, 0, 16, 1, 0, _tile([b"\xf0\x00", b"\xcc\x00"]))  # pens 3,3,1,1,2,2,0,0,...
    assert [p.image().getpixel((x, 0)) for x in (0, 2, 4, 6)] == [(0, 0, 255), (255, 0, 0), (0, 255, 0), (0, 0, 0)]
    p.begin_frame()  # a new frame: the delta pairs with what the last one left
    p.apply(0, 0, 16, 1, 2, packbits(_tile([b"\x80\x00", b"\x00\x00"])))  # XOR flips pen bit 0 of pixel 0
    assert p.image().getpixel((0, 0)) == (0, 255, 0)
    with pytest.raises(PlanarError):
        p.apply(0, 0, 16, 1, 32, b"")  # a method bit nobody defined


def test_extra_halfbrite_and_ham():
    p = Planes(6, 8, 1)
    p.flags = EHB
    p.set_palette(bytes([200, 100, 50]) + bytes(93))
    p.apply(0, 0, 8, 1, 0, bytes([0, 0, 0, 0, 0, 0x80]))  # pixel 0: pen 32
    assert p.image().getpixel((0, 0)) == (100, 50, 25)
    h = Planes(6, 8, 1)
    h.flags = HAM
    h.set_palette(bytes([10, 20, 30]) + bytes(45))
    # pixel 0: modify red (control 2) with 0xF, pixel 1: keep going with blue (control 1) 0x1
    planes = [bytes([0x80 | 0x40]), bytes([0x80]), bytes([0x80]), bytes([0x80]), bytes([0x40]), bytes([0x80])]
    h.apply(0, 0, 8, 1, 0, b"".join(planes))
    assert h.image().getpixel((0, 0)) == (0xF0 | 10 & 15, 20, 30)
    assert h.image().getpixel((1, 0)) == (0xF0 | 10 & 15, 20, 0x10 | 30 & 15)


@pytest.mark.parametrize("kind", ["yes", "rtg", "old"])
def test_planar_is_found_out_and_pictures_agree(kind):
    amiga = FakeAmiga(200, 130, profile="fast", planar=kind)
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    screen.connect()
    assert screen.planar == (kind != "old") and (screen.width, screen.height) == (200, 130)
    picture = screen.capture()
    amiga.paint(70, 70, 9, 9, 9)
    time.sleep(0.6)  # classic answers may be half a second old: let them age out
    after = screen.capture(64, 64, 64, 64)
    reference = FakeAmiga(200, 130, profile="fast", planar="old")
    reference.paint(70, 70, 9, 9, 9)
    plain = Amigentd("127.0.0.1", reference.port, timeout=5)
    assert after.tobytes() == plain.capture(64, 64, 64, 64).tobytes()
    assert picture.size == (200, 130)
    amiga.close()
    reference.close()


def test_planar_changes_come_as_xor_and_input_is_not_held_back():
    amiga = FakeAmiga(256, 128, profile="fast")
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    seen = []
    screen.listeners.append(lambda kind, payload: seen.append((kind, payload)))
    screen.capture()
    screen._send(7, 0, ord("a"), 0)  # a key, then a look: the key's tile comes back alone
    assert screen._exact(1) == b"\0"
    assert screen.poll() == 1
    assert seen[-1][0] == "planar" and len(seen[-1][1]) == 1
    x, y = amiga.cursor[0] - 8 + 2, amiga.cursor[1] + 2
    assert tuple(screen.pixels(x, y, 1, 1)) == mark_rgb(1)
    assert struct.calcsize(">HHHHBI") == 13
    amiga.close()


def test_page_gets_decoded_pixels_from_planes():
    import time as clock

    from amigent.viewer import Viewer
    from test_viewer import Page

    amiga = FakeAmiga(128, 64, profile="fast")
    viewer = Viewer("127.0.0.1", screen_port=amiga.port, ctl_port=1)
    viewer.start(0)
    page = Page(viewer.port, viewer.token)
    deadline = clock.monotonic() + 5
    while True:
        assert clock.monotonic() < deadline
        message = page.receive()
        if isinstance(message, bytes) and message[0] == 3:
            break
    x, y, w, h = struct.unpack(">HHHH", message[1:9])
    assert len(message) >= 9 + w * h * 3 and message[9:12] == bytes(screen_rgb(amiga, x, y))
    amiga.close()


def screen_rgb(amiga, x, y):
    from slowlink import _LUT_B, _LUT_G, _LUT_R

    pen = amiga.screen[y * amiga.w + x]
    return _LUT_R[pen], _LUT_G[pen], _LUT_B[pen]


def _expected(amiga):
    """The fake's screen as RGB: its pens are RGB332 bytes, and its palette says the same."""
    from PIL import Image

    from amigent.amigentd import PALETTE

    picture = Image.frombytes("P", (amiga.w, amiga.h), bytes(amiga.screen))
    picture.putpalette(PALETTE)
    return picture.convert("RGB").tobytes()


@pytest.mark.parametrize("kind", ["yes", "rtg"])
def test_every_method_decodes_to_the_amigas_pixels(kind, monkeypatch):
    import time as clock

    from amigent import planar

    used = set()
    apply = planar.Surface.apply
    monkeypatch.setattr(planar.Surface, "apply",
                        lambda self, *a: (used.add(a[4]), apply(self, *a))[1])
    amiga = FakeAmiga(320, 192, profile="fast", planar=kind)
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    screen.capture()
    amiga.busy = (40, 70, 200, 100)  # a log that scrolls by a text line
    for _ in range(4):
        clock.sleep(0.25 if kind == "yes" else 0.6)  # chunky answers may be read ahead
        with amiga.screen_lock:
            amiga.busy, busy = None, amiga.busy  # hold still while we look
        if kind == "rtg":
            clock.sleep(0.6)
        assert screen.capture().tobytes() == _expected(amiga)
        amiga.busy = busy
    if kind == "yes":
        assert {1, 10} <= used  # PackBits for the first look, scrolled deltas for the log
    else:
        assert used & {16, 17, 21}  # a desktop tile has a handful of colours
    amiga.close()


def test_shifted_partner_comes_from_before_the_frame():
    from amigent.planar import Chunky

    c = Chunky(8, 4)
    c.paint(0, 0, 8, 4, bytes(range(32)))
    c.begin_frame()
    c.apply(0, 0, 8, 2, 0, bytes(16))  # rows 0-1 zeroed first in this frame
    # rows 0-1 as a delta against the old picture one row down, zero delta: old rows 1-2
    c.apply(0, 0, 8, 2, 10, struct.pack(">h", 1) + packbits(bytes(16)))
    assert c.tile(0, 0, 8, 2) == bytes(range(8, 24))
    c.begin_frame()
    c.apply(0, 2, 8, 2, 10, struct.pack(">h", 2) + packbits(bytes(16)))  # off the bottom: zeros
    assert c.tile(0, 2, 8, 2) == bytes(16)
    c.apply(0, 0, 8, 2, 4 | 1, packbits(bytes([1] * 8 + [0] * 8)))  # row XOR: row 1 = row 0
    assert c.tile(0, 0, 8, 2) == bytes([1] * 16)
