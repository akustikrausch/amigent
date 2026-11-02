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
    p.apply(0, 0, 16, 1, 2, packbits(_tile([b"\x80\x00", b"\x00\x00"])))  # XOR flips pen bit 0 of pixel 0
    assert p.image().getpixel((0, 0)) == (0, 255, 0)
    with pytest.raises(PlanarError):
        p.apply(0, 0, 16, 1, 4, b"")  # row XOR: not before PROTOCOL.md says how


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
