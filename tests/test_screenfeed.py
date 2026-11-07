import socket
import threading
import time

import pytest

from amigent.amigentd import Amigentd, Pipeline, decode_tile, tile_fits, unpack_tile
from amigent.screenfeed import Feed, InputQueue, Planner, chunks, commands
from slowlink import FakeAmiga, SlowLink, mark_colour, mark_rgb, pack


def test_moves_squeeze_to_the_newest():
    q = InputQueue()
    for x in range(5):
        q.put({"t": "move", "x": x, "y": 0})
    q.put({"t": "buttons", "mask": 1})
    q.put({"t": "move", "x": 9, "y": 9})
    assert [op.get("x") for op in q.take()] == [4, None]  # one move at a time
    assert [op.get("x") for op in q.take()] == [9]


def test_a_held_key_queues_one_repeat_and_letting_go_drops_it():
    q = InputQueue()
    q.put({"t": "key", "code": 0x20, "down": True})
    for _ in range(30):
        q.put({"t": "key", "code": 0x20, "down": True, "repeat": True})
    assert len(q) == 1  # the first press is still waiting: repeats add nothing
    q.take()
    for _ in range(30):
        q.put({"t": "key", "code": 0x20, "down": True, "repeat": True})
    q.put({"t": "key", "code": 0x20, "down": False})
    assert q.take() == [{"t": "key", "code": 0x20, "down": False}]
    for _ in range(3):
        q.put({"t": "char", "c": "a", "repeat": True})
    assert len(q) == 1


def test_take_waits_at_a_move_while_one_is_on_its_way():
    q = InputQueue()
    q.put({"t": "char", "c": "a"})
    q.put({"t": "move", "x": 1, "y": 1})
    q.put({"t": "buttons", "mask": 1})
    assert [op["t"] for op in q.take(hold_moves=True)] == ["char"]
    assert [op["t"] for op in q.take()] == ["move", "buttons"]


def test_page_ops_become_amigentd_commands():
    assert commands({"t": "move", "x": 5000, "y": -3}, 640, 256) == [(3, 0, 639, 0)]
    assert commands({"t": "text", "s": "a\n"}, 640, 256) == [(7, 0, 97, 0), (5, 1, 0x44, 0), (5, 0, 0x44, 0)]
    assert commands({"t": "combo", "k": "lamiga+n"}, 640, 256) == [(7, 0, ord("n"), 0x40)]
    assert commands({"t": "key", "code": 0x45, "down": True, "repeat": True}, 640, 256) == [(5, 1, 0x45, 0x200)]
    assert len(commands({"t": "wheel", "dy": -9}, 640, 256)) == 10  # five clicks, down and up
    assert commands({"t": "screen", "handle": 0x12345678}, 640, 256)[-1] == "info"


def test_chunks_cover_the_screen_once():
    for width, height, size in ((640, 512, 6), (1920, 1080, 10), (640, 256, 1 << 16), (100, 70, 1)):
        seen = []
        for x, y, w, h in chunks(width, height, size):
            assert w * h <= max(size, 1) * 64 * 64 or (w, h) == (width, height)
            seen += [(tx, ty) for ty in range(y, min(y + h, height), 64) for tx in range(x, min(x + w, width), 64)]
        assert sorted(seen) == sorted({(x, y) for y in range(0, height, 64) for x in range(0, width, 64)})


def test_focus_follows_what_changed_after_a_key():
    p = Planner()
    p.note_input({"t": "move", "x": 600, "y": 20}, 0.0)
    p.note_input({"t": "char", "c": "x"}, 5.0)
    p.note_tiles([(128, 192, 64, 64, b"")], 5.2)
    boxes = p.priority(640, 256, 5.3)
    assert boxes[0] == (512, 0, 128, 128)  # around the pointer, clipped
    assert boxes[1] == (64, 192, 192, 64)  # the cursor's tile and its neighbours (the row below is off screen)
    for at in (6.0, 7.0, 8.0):  # typing on in the next tile: the focus follows
        p.note_input({"t": "char", "c": "x"}, at)
        p.note_tiles([(192, 64, 64, 64, b"")], at + 0.1)
    assert p.priority(640, 256, 8.2)[1] == (128, 64, 192, 128)
    assert p.priority(640, 256, 30.0) == boxes[:1]  # forgotten after a while


def test_tiles_unpack_and_are_checked_without_unpacking():
    assert unpack_tile(bytes([3, 7]), 2, 2) == b"\x07" * 4
    assert tile_fits(bytes([3, 7]), 2, 2) and not tile_fits(bytes([2, 7]), 2, 2)
    assert tile_fits(b"\x01\x02\x03\x04", 2, 2)  # raw
    assert decode_tile(pack(bytes(range(64)) * 64), 64, 64).getpixel((7, 0)) == (255, 0, 0)


def test_slow_link_is_slow():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def blast():
        conn, _ = server.accept()
        conn.sendall(b"x" * 20000)
        conn.close()

    threading.Thread(target=blast, daemon=True).start()
    link = SlowLink(server.getsockname()[1], 100_000, 0.05)
    start = time.perf_counter()
    got = 0
    with socket.create_connection(("127.0.0.1", link.port)) as s:
        while chunk := s.recv(65536):
            got += len(chunk)
    assert got == 20000 and time.perf_counter() - start >= 0.25


def test_pipeline_streams_frames_and_input_together():
    amiga = FakeAmiga(256, 128, profile="fast")
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    screen.connect()
    seen = []
    pipe = Pipeline(screen, on_tiles=seen.extend)
    replies = []
    pipe.frame(0, 0, 0, 0, force=True)
    pipe.command(7, 0, ord("a"), 0, on_reply=lambda ok, took: replies.append(ok))
    pipe.frame(0, 0, 0, 0)
    pipe.info()
    while pipe.busy():
        pipe.pump(1.0)
    pipe.close()
    assert replies == [True] and screen.missing_tiles() == set()
    assert len(seen) == 8 + 1  # the whole screen, then the tile the key changed
    x, y = amiga.cursor[0] - 8 + 2, amiga.cursor[1] + 2
    assert screen.capture(0, 0, 256, 128).getpixel((x, y)) == decode_tile(bytes([0, mark_colour(1)]), 1, 1).getpixel((0, 0))
    amiga.close()


@pytest.mark.parametrize("kind", ["yes", "old"])
def test_feed_shows_a_key_through_a_slow_link(kind):
    amiga = FakeAmiga(320, 256, profile="fast", planar=kind)
    link = SlowLink(amiga.port, 30_000, 0.02)
    screen = Amigentd("127.0.0.1", link.port, timeout=5)
    lock = threading.Lock()
    inputs = InputQueue()
    feed = Feed(screen, lambda: lock, inputs)
    threading.Thread(target=feed.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while (not screen.width or screen.missing_tiles()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert screen.width and not screen.missing_tiles()
    assert screen.planar == (kind == "yes")
    cx, cy = amiga.cursor
    inputs.put({"t": "char", "c": "a"})
    feed.ring()

    def shown():
        with lock:
            return tuple(screen.pixels(cx, cy, 1, 1)) == mark_rgb(1)

    start = time.monotonic()
    while not shown():
        assert time.monotonic() - start < 3
        time.sleep(0.01)
    amiga.close()
    link.close()


@pytest.mark.parametrize("answers", [True, False])
def test_feed_stays_online_while_another_client_holds_the_screen(answers, monkeypatch):
    """An agent's screen connection pushes the viewer out; the Amiga is still there."""
    import amigent.screenfeed as sf
    monkeypatch.setattr(sf, "SHARED_GRACE", 0.6)
    seen = []
    feed = Feed(None, lambda: threading.Lock(), InputQueue(),
                on_online=lambda online, reason: seen.append(online),
                alive=lambda: answers)
    feed.online = True
    feed._due = lambda now: True
    feed._broken = lambda: None

    def burst():
        raise OSError("connection closed by amigentd")

    feed._burst = burst
    threading.Thread(target=feed.run, daemon=True).start()
    time.sleep(0.3)
    assert seen == ([] if answers else [False])
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen == [False]  # a screen that stays taken is reported in the end
