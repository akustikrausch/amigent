"""Protocol tests against a scripted daemon built from the spec's examples."""

import socket
import threading

import pytest

from amigent.amigactl import AmigaCtl, AmigaError


def serve(script):
    """script(conn, reader) runs after the banner; returns the port."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        conn.sendall(b"AMIGACTL 0.8.2\n")
        script(conn, conn.makefile("rb"))
        conn.close()
        listener.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def client(script):
    amiga = AmigaCtl("127.0.0.1", serve(script), timeout=5)
    amiga.connect()
    return amiga


def test_exec_returns_rc_and_output():
    def script(conn, rd):
        assert rd.readline() == b"EXEC CD=Work: list\n"
        conn.sendall(b"OK rc=5\nDATA 6\nhello\nEND\n.\n")

    assert (r := client(script).exec("list", cd="Work:")).rc == 5 and r.output == "hello\n"


def test_binary_chunk_may_contain_framing_bytes():
    body = b"END\n.\nDATA 9\n"

    def script(conn, rd):
        rd.readline()
        conn.sendall(b"OK 13\nDATA 13\n" + body + b"END\n.\n")

    assert client(script).read_file("RAM:x") == body


def test_err_consumes_sentinel_and_connection_stays_usable():
    def script(conn, rd):
        rd.readline()
        conn.sendall(b"ERR 200 Object not found\n.\n")
        assert rd.readline() == b"SYSINFO\n"
        conn.sendall(b"OK\nexec_version=47.3\n.\n")

    amiga = client(script)
    with pytest.raises(AmigaError) as e:
        amiga.delete("RAM:nope")
    assert e.value.code == 200
    assert amiga.sysinfo() == {"exec_version": "47.3"}


def test_dir_keeps_spaces():
    def script(conn, rd):
        assert rd.readline() == b"DIR Work:My Games RECURSIVE\n"
        conn.sendall(b"OK\nFILE\tFoo Bar\t10\t00000000\t2026-01-01 10:00:00\n.\n")

    entries = client(script).list_dir("Work:My Games", recursive=True)
    assert entries[0]["name"] == "Foo Bar" and entries[0]["size"] == 10


def test_text_payload_is_dot_unstuffed():
    def script(conn, rd):
        rd.readline()
        conn.sendall(b"OK\n..x=1\ny=2\n.\n")

    assert client(script).sysinfo() == {".x": "1", "y": "2"}


def test_write_chunks_at_4096():
    seen = []

    def script(conn, rd):
        assert rd.readline() == b"WRITE RAM:x 5000\n"
        conn.sendall(b"READY\n")
        while (line := rd.readline()) != b"END\n":
            size = int(line.split()[1])
            seen.append(size)
            rd.read(size)
        conn.sendall(b"OK 5000\n.\n")

    client(script).write_file("RAM:x", bytes(5000))
    assert seen == [4096, 904]


def test_rename_sends_paths_on_own_lines():
    def script(conn, rd):
        assert [rd.readline() for _ in range(3)] == [b"RENAME\n", b"RAM:a\n", b"RAM:b\n"]
        conn.sendall(b"OK\n.\n")

    client(script).rename("RAM:a", "RAM:b")


def test_foreign_banner_is_rejected():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def run():
        conn, _ = listener.accept()
        conn.sendall(b"220 ftp ready\n")
        conn.close()

    threading.Thread(target=run, daemon=True).start()
    with pytest.raises(AmigaError):
        AmigaCtl("127.0.0.1", listener.getsockname()[1], timeout=5).connect()
