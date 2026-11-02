import json
import os
import socket
import struct
import time

from amigent.viewer import Viewer, WebSocket, _amiga_name, _join, tiles_message
from test_amigentd import serve, tile


def test_accept_key_matches_rfc_example():
    assert WebSocket.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_tiles_message_layout():
    message = tiles_message([(64, 0, 2, 1, b"\x01\x02")])
    assert message == b"\x01" + struct.pack(">HHHHI", 64, 0, 2, 1, 2) + b"\x01\x02"


def test_amiga_names():
    assert _amiga_name("a/b:c.txt") == "a_b_c.txt"
    assert _amiga_name("Grüße€.lha") == "Grüße_.lha"
    assert _join("RAM:", "x") == "RAM:x" and _join("Work:Games", "x") == "Work:Games/x"


class Page:
    """A browser stand-in speaking just enough WebSocket."""

    def __init__(self, port, token):
        self.sock = socket.create_connection(("127.0.0.1", port), 5)
        self.sock.sendall(
            f"GET /ws?t={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        assert head.startswith(b"HTTP/1.1 101")

    def _exact(self, n):
        while len(self.buf) < n:
            self.buf += self.sock.recv(65536)
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def receive(self):
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        data = self._exact(n)
        return data.decode() if b1 & 0x0F == 1 else data

    def send(self, op):
        data = json.dumps(op).encode()
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x81, 0x80 | len(data)]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))


def test_page_gets_picture_and_drives_the_mouse():
    seen = []
    full = struct.pack(">I", 2) + tile(0, 0, 3) + tile(64, 0, 5)
    port = serve([full, struct.pack(">I", 0)], seen)
    viewer = Viewer("127.0.0.1", screen_port=port)
    viewer.start(0)
    page = Page(viewer.port, viewer.token)

    messages = []
    deadline = time.monotonic() + 5
    while not any(isinstance(m, bytes) and m[0] == 1 for m in messages):
        assert time.monotonic() < deadline
        messages.append(page.receive())
    hello = json.loads(messages[0])
    assert hello["type"] == "hello"
    tiles = next(m for m in messages if isinstance(m, bytes))
    assert struct.unpack(">HHHHI", tiles[1:13])[:4] == (0, 0, 64, 64)

    page.send({"t": "move", "x": 100, "y": 20})
    page.send({"t": "buttons", "mask": 1})
    deadline = time.monotonic() + 5
    while not any(c[0] == 4 for c in seen):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    move = next(c for c in seen if c[0] == 3)
    assert struct.unpack(">HH", move[2:6]) == (100, 20)
    assert next(c for c in seen if c[0] == 4)[1] == 1


def test_wrong_token_is_refused():
    viewer = Viewer("127.0.0.1", screen_port=1)
    viewer.start(0)
    sock = socket.create_connection(("127.0.0.1", viewer.port), 5)
    sock.sendall(f"GET /?t=nope HTTP/1.1\r\nHost: 127.0.0.1:{viewer.port}\r\n\r\n".encode())
    assert sock.recv(100).startswith(b"HTTP/1.1 403")


def test_foreign_host_header_is_refused():
    viewer = Viewer("127.0.0.1", screen_port=1)
    viewer.start(0)
    sock = socket.create_connection(("127.0.0.1", viewer.port), 5)
    sock.sendall(f"GET /?t={viewer.token} HTTP/1.1\r\nHost: evil.example:{viewer.port}\r\n\r\n".encode())
    assert sock.recv(100).startswith(b"HTTP/1.1 403")


def test_windows_launcher_quotes_for_vbs(monkeypatch):
    from amigent import desktop

    monkeypatch.setattr(desktop, "_wsl", lambda: True)
    monkeypatch.setenv("WSL_DISTRO_NAME", "Debian")
    monkeypatch.setenv("AMIGENT_FLUSH_VOLUMES", "Work: SYS:")
    name, text = desktop._launcher("192.168.1.50")
    assert name == "Amigent.vbs"
    assert text.startswith('CreateObject("WScript.Shell").Run "wsl.exe -d Debian ')
    assert '""AMIGENT_FLUSH_VOLUMES=Work: SYS:""' in text
    assert text.rstrip().endswith('192.168.1.50 --exit-when-closed", 0, False')


def test_no_running_viewer_without_state(monkeypatch, tmp_path):
    import amigent.viewer as viewer

    monkeypatch.setattr(viewer, "STATE", tmp_path / "viewer.json")
    assert viewer.running_viewer("192.168.1.50") is None
