import socket
import struct
import threading
import zlib

import pytest

from amigent.amigactl import AmigaError
from amigent.channel import AmigaChannel, Commands


def ok(body: bytes = b"") -> bytes:
    return b"\0" + body


def fail(text: str, status: int = 1) -> bytes:
    raw = text.encode()
    return bytes([status]) + struct.pack(">H", len(raw)) + raw


def text(body: str) -> bytes:
    raw = body.encode("iso-8859-1")
    return ok(struct.pack(">I", len(raw)) + raw)


def fake(version=3, key=None):
    """A protocol-3 amigentd with a little in-memory file system."""
    files = {"RAM:a.txt": b"hello"}
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)

    def serve(conn):
        conn.sendall(b"AMGT" + struct.pack(">HH", version, 1 if key else 0))
        rd = conn.makefile("rb")
        while len(head := rd.read(10)) == 10:
            cmd, arg, x, y, w, h = struct.unpack(">BBHHHH", head)
            if cmd == 17:
                given = rd.read(x)
                conn.sendall(b"\0" if given == key else b"\1")
                continue
            if cmd == 20:
                conn.sendall(b"\0" if version >= 3 else b"\1")
                continue
            path = rd.read(x).decode("iso-8859-1")
            if cmd == 21:
                where = rd.read(y)
                out = f"ran {path} in {where.decode() or '-'}".encode()
                conn.sendall(ok(struct.pack(">iI", 5, len(out)) + out))
            elif cmd == 22:
                if path in files:
                    conn.sendall(ok(struct.pack(">I", len(files[path])) + files[path]))
                else:
                    conn.sendall(fail("object not found", 2))
            elif cmd == 23:
                data = rd.read(w << 16 | h)
                files[path] = (files.get(path, b"") if arg else b"") + data
                conn.sendall(ok())
            elif cmd == 24:
                conn.sendall(text("FILE\ta.txt\t5\t00000000\t2026-11-02 10:00:00\n"
                                  "DIR\tsub\t0\t00000000\t2026-11-02 10:00:00\n" if path == "RAM:" else
                                  "FILE\tb.txt\t1\t00000000\t2026-11-02 10:00:00\n"))
            elif cmd == 27:
                new = rd.read(y).decode()
                files[new] = files.pop(path)
                conn.sendall(ok())
            elif cmd == 29:
                conn.sendall(ok(struct.pack(">I", zlib.crc32(files[path]))))
            elif cmd == 30:
                conn.sendall(text("chip_free=100\nkickstart=47\n"))
            elif cmd == 31:
                conn.sendall(text("RAM Disk:\t10\t20\t30\t512\n"))
            else:
                conn.sendall(fail("unknown"))
        conn.close()

    def run():
        while True:
            conn, _ = listener.accept()
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1], files


def test_files_and_commands():
    port, files = fake()
    ch = AmigaChannel("127.0.0.1", port, timeout=5, key="")
    assert ch.read_file("RAM:a.txt") == b"hello"
    ch.write_file("RAM:b.txt", b"12")
    ch.write_file("RAM:b.txt", b"34", append=True)
    assert files["RAM:b.txt"] == b"1234"
    assert ch.checksum("RAM:b.txt") == zlib.crc32(b"1234")
    ch.rename("RAM:b.txt", "RAM:c.txt")
    assert "RAM:c.txt" in files
    result = ch.exec("Version", cd="SYS:")
    assert result.rc == 5 and result.output == "ran Version in SYS:"
    assert ch.sysinfo()["chip_free"] == "100"
    assert ch.volumes()[0]["name"] == "RAM Disk:" and ch.volumes()[0]["free"] == 20


def test_errors_carry_a_code():
    port, _ = fake()
    ch = AmigaChannel("127.0.0.1", port, timeout=5, key="")
    with pytest.raises(AmigaError) as e:
        ch.read_file("RAM:nothing")
    assert e.value.code == 2 and "not found" in str(e.value)
    assert ch.read_file("RAM:a.txt") == b"hello"  # the channel stays usable


def test_recursive_listing():
    port, _ = fake()
    names = [e["name"] for e in AmigaChannel("127.0.0.1", port, timeout=5, key="").list_dir("RAM:", True)]
    assert names == ["a.txt", "sub", "sub/b.txt"]


def test_key_is_sent():
    port, _ = fake(key=b"sesam")
    assert AmigaChannel("127.0.0.1", port, timeout=5, key="sesam").read_file("RAM:a.txt") == b"hello"
    with pytest.raises(AmigaError, match="refused the key"):
        AmigaChannel("127.0.0.1", port, timeout=5, key="nope").connect()


def test_old_amigentd_falls_back_to_amigactld():
    port, _ = fake(version=2)
    banner = socket.socket()
    banner.bind(("127.0.0.1", 0))
    banner.listen(1)

    def run():
        conn, _ = banner.accept()
        conn.sendall(b"AMIGACTL 0.8.2\n")
        conn.recv(100)
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    commands = Commands("127.0.0.1", banner.getsockname()[1], port, timeout=5)
    commands.connect()
    assert commands.banner.startswith("AMIGACTL")


def test_tls_is_pinned(tmp_path, monkeypatch):
    import ssl
    import subprocess

    from amigent import tls

    monkeypatch.setattr(tls, "FOLDER", tmp_path)
    made = tls.make_certificate("127.0.0.1")
    if made is None:
        pytest.skip("no openssl")
    for_amiga, certificate = made
    (tmp_path / "server.pem").write_bytes(for_amiga)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "server.pem")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)
    port = listener.getsockname()[1]

    def run():
        while True:
            conn, _ = listener.accept()
            try:
                with context.wrap_socket(conn, server_side=True) as secure:
                    secure.sendall(b"AMGT")
            except (OSError, ssl.SSLError):
                pass

    threading.Thread(target=run, daemon=True).start()
    assert not tls.wanted("127.0.0.1")
    tls.remember("127.0.0.1", certificate)
    with tls.connect("127.0.0.1", port - 1, 5) as sock:
        assert sock.recv(4) == b"AMGT"
    other = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                            "ec_paramgen_curve:prime256v1", "-nodes", "-subj", "/CN=x", "-keyout",
                            str(tmp_path / "k"), "-out", str(tmp_path / "c")], capture_output=True)
    assert other.returncode == 0
    tls.remember("127.0.0.1", (tmp_path / "c").read_bytes())
    with pytest.raises(ssl.SSLError):
        tls.connect("127.0.0.1", port - 1, 5)


def test_a_closed_plain_port_says_what_to_do():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        conn.sendall(b"AMGT" + struct.pack(">HH", 3, 1 | 2 | 4))
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    with pytest.raises(AmigaError, match="only encrypted"):
        AmigaChannel("127.0.0.1", listener.getsockname()[1], timeout=5, key="k").connect()


def test_lock_plain_touches_only_the_startup_line():
    from amigent import tls

    class Files:
        def __init__(self, startup):
            self.files = {"S:User-Startup": startup.encode()}

        def read_file(self, path):
            return self.files[path]

        def write_file(self, path, data):
            self.files[path] = data

    disk = Files("; mine\nRUN >NIL: C:amigentd\nAssign Foo: SYS:Foo\n")
    assert tls.lock_plain(disk, "SYS:amigent")
    assert disk.files["S:User-Startup"] == b"; mine\nRUN >NIL: C:amigentd TLSONLY\nAssign Foo: SYS:Foo\n"
    assert "SYS:amigent/amigent.tlsonly" in disk.files
    again = Files("RUN >NIL: C:amigentd TLSONLY\n")
    assert tls.lock_plain(again, "SYS:amigent") and list(again.files) == ["S:User-Startup"]
