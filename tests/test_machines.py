import socket
import struct
import threading

from amigent import machines, secret, tls


def test_names_and_addresses(private_config):
    assert machines.default() == "" and machines.address("10.0.0.5") == "10.0.0.5"
    machines.remember("A1200", "10.0.0.5")
    assert machines.default() == "A1200" and machines.pick() == "A1200"
    assert machines.address("a1200") == "10.0.0.5" and machines.name_of("10.0.0.5") == "A1200"
    machines.remember("A1200", "10.0.0.9")
    assert machines.address("A1200") == "10.0.0.9"
    assert machines.name_of("10.0.0.5") == "A1200"  # the old address still means this Amiga
    assert machines.stems("A1200") == ["A1200", "10.0.0.9", "10.0.0.5"]
    machines.remember("A500", "10.0.0.7")
    assert machines.default() == "A1200"
    assert machines.rename("A500", "A500 Plus") and machines.address("A500 Plus") == "10.0.0.7"
    assert machines.forget("A1200") and machines.default() == "A500 Plus"


def test_key_and_certificate_follow_the_amiga(private_config):
    secret.save_key("geheim", "10.0.0.5")
    tls.remember("10.0.0.5", b"cert")
    machines.remember("A1200", "10.0.0.5")
    machines.move("A1200", "10.0.0.9")
    assert secret.load_key("A1200") == "geheim" and secret.own_key("10.0.0.9") == "geheim"
    assert tls.pinned("A1200").read_bytes() == b"cert" and tls.wanted("10.0.0.9")


def test_an_address_from_before_names_gets_one(private_config):
    machines.move("10.0.0.5", "10.0.0.9")
    assert machines.name_of("10.0.0.5") == "Amiga" and machines.address("10.0.0.5") == "10.0.0.9"


def fake_amigentd(key: str):
    """amigentd's hello and AUTH, on a free port of 127.0.0.1."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)

    def run():
        while True:
            conn, _ = listener.accept()
            with conn:
                conn.sendall(b"AMGT" + struct.pack(">HH", 3, 1))
                conn.settimeout(2)
                try:
                    head = conn.recv(10)
                    if len(head) < 10:
                        continue
                    length = struct.unpack(">BBHHHH", head)[2]
                    given = conn.recv(length).decode()
                    conn.sendall(b"\0" if given == key else b"\1")
                except OSError:
                    pass

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def test_found_again_only_with_its_own_key(private_config, monkeypatch):
    port = fake_amigentd("geheim")
    monkeypatch.setattr(machines, "GREETING_PORT", port)
    monkeypatch.setattr(machines, "scan", lambda: ["127.0.0.1"])
    machines.remember("A1200", "127.0.0.2")
    secret.save_key("falsch", "A1200")
    assert machines.relocate("A1200") is None  # someone else's Amiga
    machines._searched.clear()
    secret.save_key("geheim", "A1200")
    assert machines.relocate("A1200") == "127.0.0.1"
    assert machines.address("A1200") == "127.0.0.1"
    assert machines.relocate("A1200") is None  # not again right away


def test_connect_finds_the_new_address(private_config, monkeypatch):
    port = fake_amigentd("geheim")
    monkeypatch.setattr(machines, "GREETING_PORT", port)
    monkeypatch.setattr(machines, "scan", lambda: ["127.0.0.1"])
    machines.remember("A1200", "127.0.0.2")  # nothing listens there
    secret.save_key("geheim", "A1200")
    with tls.connect("A1200", port, 2) as sock:
        assert sock.recv(4) == b"AMGT"
    assert machines.address("A1200") == "127.0.0.1"


def test_one_lookup_for_keys_and_certificates(private_config):
    from amigent import roster
    machines.remember("A1200", "localhost")
    assert machines.find is roster.find
    assert roster.aliases("localhost") == ["A1200", "localhost", "127.0.0.1"]  # DNS joins in last
    assert roster.aliases("nowhere.invalid") == ["nowhere.invalid"]
