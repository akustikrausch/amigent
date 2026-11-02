import socket
import threading

from amigent import net, platform_util


def listener(say: bytes):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()

    def answer():
        conn, _ = server.accept()
        conn.sendall(say)
        conn.close()
        server.close()

    threading.Thread(target=answer, daemon=True).start()
    return server.getsockname()[1]


def test_greet_hears_the_hello():
    port = listener(b"AMGT\0\3\0\1")
    assert net.greet("127.0.0.1", port, 2) == b"AMGT\0\3\0\1"


def test_greet_is_quiet_when_nobody_is_home():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound, never listening: the door stays shut
    assert net.greet("127.0.0.1", port, 1) == b""


def test_lan_address_as_the_peer_sees_us():
    port = listener(b"")
    assert net.lan_address("127.0.0.1", port) == "127.0.0.1"


def test_windows_exe_falls_back_to_the_bare_name():
    assert platform_util.windows_exe("no-such-program-4711.exe") == "no-such-program-4711.exe"
    assert isinstance(platform_util.in_wsl(), bool)
