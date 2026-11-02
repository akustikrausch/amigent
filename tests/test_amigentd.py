import socket
import struct
import threading

from amigent.amigentd import Amigentd, decode_tile

W, H = 128, 64


def tile(x, y, colour):
    body = bytes([255, colour]) * 16  # 16 runs of 256 = 64x64
    return struct.pack(">HHHHI", x, y, 64, 64, len(body)) + body


def serve(frames, seen, drop_after=None):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def run():
        served = 0
        while True:
            conn, _ = listener.accept()
            conn.sendall(b"AMGT\x00\x01\x00\x00")
            while len(data := conn.recv(10)) == 10:
                seen.append(data)
                if data[0] == 1:
                    conn.sendall(struct.pack(">HHHH", W, H, 64, 0))
                elif data[0] == 2:
                    served += 1
                    if served == drop_after:
                        break
                    conn.sendall(frames[min(served - 1, len(frames) - 1)])
                else:
                    conn.sendall(b"\0")
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def test_rle_and_raw_tiles_decode():
    assert decode_tile(bytes([3, 7]), 2, 2).getpixel((1, 1)) == (255, 0, 0)
    raw = decode_tile(bytes([7, 7, 7, 7]), 2, 2)
    assert raw.getpixel((0, 0)) == (255, 0, 0)


def test_changed_tiles_paint_over_the_cached_frame():
    full = struct.pack(">I", 2) + tile(0, 0, 7) + tile(64, 0, 7)
    change = struct.pack(">I", 1) + tile(64, 0, 0)
    same = struct.pack(">I", 0)
    seen = []
    client = Amigentd("127.0.0.1", serve([full, change, same], seen), timeout=5)
    first = client.capture()
    assert first.getpixel((10, 10)) == (255, 0, 0)
    second = client.capture()
    assert second.getpixel((10, 10)) == (255, 0, 0)
    assert second.getpixel((70, 10)) == (0, 0, 0)
    third = client.capture(60, 0, 8, 8)
    assert third.size == (8, 8)
    frames = [d for d in seen if d[0] == 2]
    assert [f[1] for f in frames] == [1, 0, 0]


def test_capture_reconnects_after_a_dropped_link():
    full = struct.pack(">I", 2) + tile(0, 0, 7) + tile(64, 0, 7)
    client = Amigentd("127.0.0.1", serve([full], [], drop_after=1), timeout=5)
    assert client.capture().size == (W, H)


def test_combo_sends_qualifier():
    seen = []
    client = Amigentd("127.0.0.1", serve([b""], seen), timeout=5)
    client.connect()
    client.press("lamiga+n")
    client.press("return")
    assert struct.unpack(">BBHHHH", seen[-3]) == (7, 0, ord("n"), 0x40, 0, 0)
    assert struct.unpack(">BBHHHH", seen[-2])[:3] == (5, 1, 0x44)
    assert struct.unpack(">BBHHHH", seen[-1])[:3] == (5, 0, 0x44)
