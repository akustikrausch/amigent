"""AMIGENT_REQUIRE_TLS and AMIGENT_READ_ONLY: nothing in the clear, nothing that changes the Amiga.

The plain ports here are tripwires: they count who knocks, and a knock fails
the test. The TLS port serves a FakeAmiga behind a test certificate. The MCP
server runs as a child process over stdio, the way an MCP client starts it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from mcp.types import InitializeResult

import amigent.server as server
from amigent import machines, net, others, power, roster, tls
from amigent.amigentd import Amigentd, AmigentdError
from amigent.fleet import Fleet
from amigent.sessions import MachineSession
from amigent.channel import (CMD_CHANNEL, COMPRESS, DIR, LOADED, NOTE, POKE, SYSINFO, TASKCTL, TASKS,
                             TEXT, AmigaChannel, Commands, ReadOnlyRefused, read_only_allows)
from amigent.errors import AmigaError
from fakeamiga import FakeAmiga

HOST = "127.0.0.1"
INSTRUCTED = "instructions" in InitializeResult.model_fields  # mcp 1.2 sends none
KEY = "canary-key-3f9b0c71"
SRC = Path(__file__).resolve().parents[1] / "src"
FORBIDDEN = {  # tool: arguments that would change the Amiga if the call got through
    "amiga_exec": {"command": "Delete SYS:#? ALL QUIET"},
    "amiga_write_text": {"path": "SYS:S/Startup-Sequence", "text": "gone"},
    "amiga_delete": {"path": "SYS:C/Dir"},
    "amiga_memory_write": {"address": "$100", "hex_bytes": "4e71"},
    "amiga_trace": {"action": "set", "address": "$f80000"},
    "amiga_restart": {},
    "amiga_type": {"text": "format drive df0:"},
    "amiga_status": {},
    "amiga_brand_new_tool": {},
    # added in 1.5.0, never audited for read-only
    "amiga_machines": {},
    "amiga_update": {"apply": True},
    "amiga_emu68_update": {},
    "amiga_pi_network": {"action": "setup"},
    "amiga_pistorm": {},
    "amiga_serial_log": {},
    "amiga_wait_task": {"name": "x"},
}


# ---------------------------------------------------------------- the stage

class Tripwire:
    """A plain port that only listens: says a key-wanting hello and keeps what it hears."""

    def __init__(self, listener: socket.socket | None = None):
        if listener is None:
            listener, spare = _neighbours()
            spare.close()
        self.listener = listener
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.knocks = 0
        self.heard = b""
        self.log: list[tuple] = []  # (peer, what it said) per knock
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, peer = self.listener.accept()
            except OSError:
                return
            self.knocks += 1
            said = b""
            try:
                conn.sendall(b"AMGT" + struct.pack(">HH", 6, 1))
                conn.settimeout(1)
                said = conn.recv(4096)
            except OSError:
                pass
            self.heard += said
            self.log.append((peer, said[:40]))
            conn.close()


class TlsFront:
    """amigentd's TLS port: the handshake with certfile, then fake behind it (plain=True skips TLS)."""

    def __init__(self, listener: socket.socket, fake: FakeAmiga, certfile: Path, plain: bool = False):
        self.listener, self.fake, self.plain = listener, fake, plain
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(certfile))
        self.accepted = self.handshakes = 0
        self.refusing = False
        self.delay = 0.0  # seconds before the handshake: a slow AmiSSL
        listener.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def refuse(self):
        """Shut the port so a dial is refused, on every system."""
        self.refusing = True
        port = self.listener.getsockname()[1]
        with contextlib.suppress(OSError):  # Linux: wakes the accept() that keeps it listening
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection((HOST, port), 5).close()  # still listening: wakes accept()
            except ConnectionRefusedError:
                return
            except OSError:
                pass
            time.sleep(0.05)
        raise AssertionError(f"port {port} still takes connections")

    def _accept(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            if self.refusing:
                conn.close()
                return
            self.accepted += 1
            threading.Thread(target=self._one, args=(conn,), daemon=True).start()

    def _one(self, conn):
        try:
            if self.delay:
                threading.Event().wait(self.delay)  # not time.sleep, which tests may shorten
            if not self.plain:
                conn = self.context.wrap_socket(conn, server_side=True)
                self.handshakes += 1
            self.fake._serve(conn)
        except (OSError, ssl.SSLError):
            conn.close()


class KeyedFake(FakeAmiga):
    """amigentd that wants a key, speaks protocol 6, and answers TEXT, LOADED and TASKS (empty)."""

    def __init__(self, key: str = KEY):
        super().__init__()
        self.protocol, self.key, self.keys_heard = 6, key, []
        self.add("SYS:C/Dir", b"dir")
        self.add("SYS:S/Startup-Sequence", b"; boot\n")

    def _serve(self, conn):
        conn.sendall(b"AMGT" + struct.pack(">HH", self.protocol, 1))
        rd = conn.makefile("rb")
        try:
            cmd, _, x, _, _, _ = struct.unpack(">BBHHHH", rd.read(10))
            if cmd != 17:
                return
            heard = rd.read(x).decode("iso-8859-1")
            self.keys_heard.append(heard)
            if heard != self.key:
                conn.sendall(b"\1")
                return
            conn.sendall(b"\0")
            channel = False
            while len(head := rd.read(10)) == 10:
                cmd, arg, x, y, w, h = struct.unpack(">BBHHHH", head)
                self.counts[cmd] = self.counts.get(cmd, 0) + 1
                if cmd == CMD_CHANNEL:
                    channel = True
                    conn.sendall(b"\0")
                elif channel:
                    conn.sendall(self._channel(rd, cmd, arg, x, y, w, h))
        except (OSError, struct.error):
            pass
        finally:
            conn.close()

    def _channel(self, rd, cmd, arg, x, y, w, h):
        if cmd in (TEXT, LOADED, TASKS):
            rd.read(x)
            return self.text("")
        return super()._channel(rd, cmd, arg, x, y, w, h)


def _bound(port: int) -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, port))
    return s


def _neighbours() -> tuple[socket.socket, socket.socket]:
    """Two bound listeners on ports n and n + 1: amigentd's plain port and its TLS port.

    Below every OS's ephemeral range on purpose: a client another test left
    redialling its old port 0 address can never land on a tripwire here."""
    for _ in range(200):
        port = random.randrange(20000, 31998)
        try:
            plain = _bound(port)
        except OSError:
            continue
        try:
            return plain, _bound(port + 1)
        except OSError:
            plain.close()
    pytest.skip("no two neighbouring free ports")


def _certificate(folder: Path, name: str) -> tuple[Path, bytes]:
    """(certificate and key for the server, certificate for the pin), or a skip without openssl."""
    made = tls.make_certificate(HOST)
    if not made:
        pytest.skip("no openssl")
    both = folder / f"{name}.pem"
    both.write_bytes(made[0])
    return both, made[1]


@pytest.fixture
def stage(tmp_path, monkeypatch):
    """Tripwires on the plain and amigactld ports, a pinned TLS FakeAmiga on the port after."""
    def build(fake=None, pin="same", plain_tls=False):
        plain, tls_port = _neighbours()
        served, certificate = _certificate(tmp_path, "served")
        if pin == "other":
            _, certificate = _certificate(tmp_path, "other")
        if pin:
            tls.remember(HOST, certificate)
        fake = fake or KeyedFake()
        world = {"wire": Tripwire(plain), "ctl": Tripwire(), "fake": fake,
                 "front": TlsFront(tls_port, fake, served, plain=plain_tls),
                 "certificate": certificate, "served": served}
        world["port"] = world["wire"].port
        return world
    monkeypatch.setenv("AMIGENT_KEY", KEY)
    return build


def _no_plain(world) -> None:
    assert world["wire"].knocks == 0, f"a plain connection reached amigentd's port: {world['wire'].log}"
    assert world["ctl"].knocks == 0, f"a plain connection reached amigactld's port: {world['ctl'].log}"
    assert KEY.encode() not in world["wire"].heard + world["ctl"].heard


# ---------------------------------------------------------------- TLS only

def test_require_tls_lists_over_tls_and_never_dials_plain(stage, monkeypatch):
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    world = stage()
    ctl = Commands(HOST, world["ctl"].port, world["port"], timeout=5)
    names = [e["name"] for e in ctl.list_dir("SYS:")]
    assert names == ["C", "S"]
    assert ctl.is_tls and world["front"].handshakes == 1
    assert world["fake"].keys_heard == [KEY]
    _no_plain(world)


def test_require_tls_retry_is_a_new_authenticated_tls_line(stage, monkeypatch):
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    world = stage()
    ctl = Commands(HOST, world["ctl"].port, world["port"], timeout=5)
    ctl.connect()
    ctl.list_dir("SYS:")
    ctl.close()  # the link drops; the next call dials again
    ctl.connect()
    assert ctl.list_dir("SYS:C")[0]["name"] == "Dir"
    assert world["front"].handshakes == 2 and world["fake"].keys_heard == [KEY, KEY]
    _no_plain(world)


@pytest.mark.parametrize("case", ["no pin", "TLS off", "plain on the TLS port", "wrong certificate"])
def test_require_tls_fails_closed(stage, monkeypatch, case):
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    relocated = []
    monkeypatch.setattr(machines, "relocate", lambda host: relocated.append(host))
    world = stage(pin=None if case == "no pin" else "other" if case == "wrong certificate" else "same",
                  plain_tls=case == "plain on the TLS port")
    if case == "TLS off":
        monkeypatch.setenv("AMIGENT_TLS", "off")
    ctl = Commands(HOST, world["ctl"].port, world["port"], timeout=5)
    with pytest.raises(AmigaError) as e:
        ctl.connect()
    if case in ("no pin", "TLS off"):
        assert "AMIGENT_REQUIRE_TLS" in str(e.value) and world["front"].accepted == 0
    else:
        assert world["front"].accepted == 1  # the TLS port was tried once, and only it
    assert ctl.backend is None  # no amigactld fallback either
    assert world["fake"].keys_heard == [] and relocated == []
    _no_plain(world)


def test_require_tls_screen_link_refuses_too(stage, monkeypatch):
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    world = stage(pin=None)
    with pytest.raises(AmigentdError):
        Amigentd(HOST, world["port"], timeout=5).connect()
    _no_plain(world)


def test_require_tls_never_sweeps_or_knocks(stage, monkeypatch):
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    swept = []
    monkeypatch.setattr(machines, "scan", lambda: swept.append(1) or [])
    assert machines.relocate(HOST) is None and swept == []
    probed = []
    monkeypatch.setattr(others, "listening", lambda host, port, timeout=1.0: probed.append(port))
    others.scan(None, "10.0.0.5", {})
    assert probed == []
    world = stage()
    assert power.greet(HOST, world["port"], 3, 3)[:4] == b"AMGT"  # amiga_restart's "is it back"
    assert world["front"].handshakes == 1
    _no_plain(world)


def test_require_tls_reads_no_plain_hello_when_the_tls_port_refuses(stage, monkeypatch):
    """tls._why_tls_refused reads the plain hello to explain a shut TLS port; not under the switch."""
    monkeypatch.setattr(machines, "relocate", lambda host: None)
    world = stage()
    world["front"].refuse()
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    with pytest.raises(OSError):
        tls.connect(HOST, world["port"], 3)
    with pytest.raises(AmigaError):
        Commands(HOST, world["ctl"].port, world["port"], timeout=3).connect()
    _no_plain(world)
    monkeypatch.delenv("AMIGENT_REQUIRE_TLS")
    with pytest.raises(OSError):
        tls.connect(HOST, world["port"], 3)
    assert world["wire"].knocks == 1 and world["wire"].heard == b""  # default: looked, said nothing


def test_require_tls_does_not_search_for_a_saved_amiga(stage, monkeypatch):
    """Only saved Amigas get searched for after a failed dial, and the search knocks on plain ports."""
    world = stage(pin=None)
    saved = roster.remember("A1200", HOST)["id"]
    tls.remember(saved, world["certificate"])
    world["front"].refuse()
    searched = []
    monkeypatch.setattr(machines, "relocate", lambda host: searched.append(host))
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    with pytest.raises(OSError):
        tls.connect(saved, world["port"], 3)
    assert searched == []
    _no_plain(world)
    monkeypatch.delenv("AMIGENT_REQUIRE_TLS")
    with pytest.raises(OSError):
        tls.connect(saved, world["port"], 3)
    assert searched == [saved]  # without the switch it would have looked


@pytest.mark.parametrize("pin", ["same", None])
def test_require_tls_explicit_address_dials_only_tls(stage, monkeypatch, pin):
    """amiga_pi_network's channel to the Pi's new address goes through tls._dial, not around it."""
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    world = stage(pin=pin)
    ch = AmigaChannel(HOST, world["port"], timeout=5, address=HOST)
    if pin:
        assert [e["name"] for e in ch.list_dir("SYS:")] == ["C", "S"]
        ch.close()
        assert world["front"].handshakes == 1
    else:
        with pytest.raises(AmigaError, match="AMIGENT_REQUIRE_TLS"):
            ch.connect()
        assert world["front"].accepted == 0
    _no_plain(world)


@pytest.mark.parametrize("required", [True, False])
def test_reachability_checks_greet_where_the_switch_says(monkeypatch, required):
    """amiga_machines and the overview: the TLS hello under the switch, the plain one without."""
    if required:
        monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    greeted = []
    entry = {"id": "amiga-a1", "name": "A1", "host": "10.0.0.5", "stop": threading.Event(),
             "online": False, "error": ""}

    def plain(host, *args, **kwargs):
        greeted.append(("plain", host))
        entry["stop"].set()
        return b"AMGT\0\6\0\1"

    def encrypted(host, *args, **kwargs):
        greeted.append(("tls", host))
        entry["stop"].set()
        return b"AMGT\0\6\0\1"

    monkeypatch.setattr(net, "greet", plain)
    monkeypatch.setattr(tls, "greet", encrypted)
    monkeypatch.setattr(machines, "load", lambda: [{k: entry[k] for k in ("id", "name", "host")}])
    assert json.loads(server.amiga_machines())[0]["online"] is True
    entry["stop"].clear()
    watcher = object.__new__(Fleet)
    watcher.saved, watcher.screen_port, watcher.overview_until = False, 6801, 0.0
    watcher._watch(entry)
    kind = "tls" if required else "plain"
    assert greeted == [(kind, "amiga-a1" if required else "10.0.0.5"), (kind, "10.0.0.5")]


def test_default_mode_keeps_the_plain_paths(stage, monkeypatch):
    """Without the switch nothing changes: no pin is plain, a dead channel falls back to amigactld."""
    world = stage(pin=None)
    fake = FakeAmiga()
    fake.add("RAM:x", b"1")
    assert [e["name"] for e in Commands(HOST, world["ctl"].port, fake.port, timeout=5).list_dir("RAM:")] == ["x"]
    with pytest.raises(AmigaError):
        Commands(HOST, world["ctl"].port, world["port"], timeout=5).connect()
    assert world["ctl"].knocks == 1


# ---------------------------------------------------------------- TLS only: gone, and back

@pytest.fixture
def quick(monkeypatch):
    """AMIGENT_REQUIRE_TLS with short pauses between looks and a short handshake allowance."""
    monkeypatch.setenv("AMIGENT_REQUIRE_TLS", "1")
    nap = time.sleep
    monkeypatch.setattr(power.time, "sleep", lambda s: nap(0.2))
    monkeypatch.setattr(tls, "HANDSHAKE_WAIT", 2.0)


def _stalled(tmp_path):
    """A plain tripwire and, on the port after, a pinned TLS port that takes the
    connection and never answers the handshake."""
    plain, stalled = _neighbours()
    stalled.listen(8)  # the system completes the connection; nobody ever accepts it
    tls.remember(HOST, _certificate(tmp_path, "stalled")[1])
    return Tripwire(plain), stalled


def test_require_tls_restart_checks_on_a_live_and_a_shut_port(stage, quick):
    world = stage()
    port = world["port"]
    assert power.wait_for_port(HOST, port, 5)
    assert not power.wait_gone(HOST, port, wait=1)
    world["front"].refuse()
    assert power.wait_gone(HOST, port, wait=5)
    assert not power.wait_for_port(HOST, port, 1)
    assert world["fake"].keys_heard == []  # looking sends no key
    _no_plain(world)


def test_require_tls_a_stalled_handshake_is_neither_gone_nor_back(tmp_path, quick):
    wire, stalled = _stalled(tmp_path)
    try:
        assert not power.wait_gone(HOST, wire.port, wait=1), "a stalled handshake is not a restart"
        assert not power.wait_for_port(HOST, wire.port, 1)
    finally:
        stalled.close()
    assert wire.knocks == 0


@pytest.mark.parametrize("case", ["wrong certificate", "no TLS behind the port"])
def test_require_tls_a_failed_handshake_is_neither_gone_nor_back(stage, quick, case):
    world = stage(pin="other" if case == "wrong certificate" else "same",
                  plain_tls=case == "no TLS behind the port")
    assert not power.wait_gone(HOST, world["port"], wait=1)
    assert not power.wait_for_port(HOST, world["port"], 1)
    assert world["front"].accepted >= 2 and world["fake"].keys_heard == []
    _no_plain(world)


def test_require_tls_wait_for_port_waits_out_a_slow_handshake(stage, quick, monkeypatch):
    monkeypatch.setattr(tls, "HANDSHAKE_WAIT", 30.0)
    world = stage()
    world["front"].delay = 6.0  # longer than the 5 s connect timeout
    assert power.wait_for_port(HOST, world["port"], 20)
    assert world["front"].handshakes == 1
    _no_plain(world)


def test_require_tls_wait_for_port_sees_the_amiga_come_back(stage, quick):
    world = stage()
    port = world["port"]
    world["front"].refuse()
    back = threading.Timer(1.0, lambda: world.update(
        front=TlsFront(_bound(port + 1), world["fake"], world["served"])))
    back.start()
    try:
        assert power.wait_for_port(HOST, port, 20)
    finally:
        back.cancel()
    assert world["front"].handshakes == 1
    _no_plain(world)


def test_require_tls_restart_is_not_inferred_from_a_stalled_handshake(tmp_path, quick, monkeypatch,
                                                                      machine_session):
    wire, stalled = _stalled(tmp_path)
    monkeypatch.setattr(machine_session, "host", HOST)
    monkeypatch.setattr(server, "SCREEN_PORT", wire.port)
    monkeypatch.setattr(server, "reboot", lambda *a: None)  # C:Reboot ran, or so it says
    monkeypatch.setattr(server, "wait_gone", lambda host, port: power.wait_gone(host, port, wait=1))
    monkeypatch.setattr(server, "wait_for_port", lambda *a: pytest.fail("took the restart as done"))
    try:
        with pytest.raises(AmigaError, match="still up"):
            server.amiga_restart()
    finally:
        stalled.close()
    assert wire.knocks == 0


def test_default_mode_restart_checks_stay_plain(stage, monkeypatch):
    """Without the switch, gone and back are the plain hello, as before."""
    world = stage()
    assert power.wait_for_port(HOST, world["port"], 5)
    assert not power.wait_gone(HOST, world["port"], wait=1)
    assert world["wire"].knocks >= 2 and world["front"].accepted == 0


# ---------------------------------------------------------------- read only, on the wire

def test_read_only_opcodes_follow_the_protocol():
    assert read_only_allows(6, TASKS) and read_only_allows(6, LOADED)
    assert not read_only_allows(6, TASKCTL) and not read_only_allows(6, POKE)
    assert not any(read_only_allows(5, c) for c in (LOADED, TASKS, TASKCTL))  # protocol 6 only
    assert all(read_only_allows(p, c) for p in (3, 5, 6) for c in (DIR, SYSINFO, TEXT, COMPRESS))


def test_read_only_channel_refuses_before_the_wire():
    fake = FakeAmiga()
    fake.add("RAM:keep", b"1")
    ch = AmigaChannel(HOST, fake.port, timeout=5, read_only=True)
    assert [e["name"] for e in ch.list_dir("RAM:")] == ["keep"]
    for attempt in (lambda: ch.exec("Delete RAM:keep"), lambda: ch.write_file("RAM:new", b"x"),
                    lambda: ch.delete("RAM:keep"), lambda: ch.rename("RAM:keep", "RAM:moved"),
                    lambda: ch.makedir("RAM:dir"), lambda: ch.read_file("RAM:keep"),
                    lambda: ch.pause_screen(10)):
        with pytest.raises(ReadOnlyRefused):
            attempt()
    ch.note("not drawn")  # NOTE is dropped quietly
    assert set(fake.counts) <= {CMD_CHANNEL, SYSINFO, DIR}
    assert fake.files == {"RAM:keep": b"1"} and not fake.dirs and not fake.pause_for


def _counted(fake: FakeAmiga, cmd: int, seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cmd in fake.counts:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("read_only", [True, False])
def test_read_only_channel_sends_no_note(read_only):
    fake = FakeAmiga()
    fake.protocol, fake.info["note"] = 6, "1"  # an amigent-monitor that takes NOTE lines
    ch = AmigaChannel(HOST, fake.port, timeout=5, read_only=read_only)
    ch.connect()
    ch.note("list SYS:")
    assert _counted(fake, NOTE, 2.0 if not read_only else 0.5) is not read_only
    ch.close()


def test_read_only_server_sends_no_note(monkeypatch, machine_session):
    lines = []
    link = types.SimpleNamespace(connected=True, backend=types.SimpleNamespace(note=lines.append))
    monkeypatch.setattr(machine_session, "ctl", link)
    monkeypatch.setattr(server, "READ_ONLY", True)
    assert server._note("list SYS:") is False and lines == []
    monkeypatch.setattr(server, "READ_ONLY", False)
    assert server._note("list SYS:") is True and lines == ["list SYS:"]


def test_read_only_takes_no_amigactld_and_no_screen_link(monkeypatch):
    wire = Tripwire()
    with pytest.raises(AmigaError):
        Commands(HOST, wire.port, 9, timeout=2, read_only=True).connect()
    assert wire.knocks == 0
    monkeypatch.setenv("AMIGENT_READ_ONLY", "1")
    with pytest.raises(AmigentdError):
        Amigentd(HOST, wire.port).connect()
    assert wire.knocks == 0


def test_read_only_reaches_every_machine_session(monkeypatch):
    """machine= picks another Amiga; its session is held to the same list."""
    monkeypatch.setenv("AMIGENT_READ_ONLY", "1")
    session = MachineSession("10.0.0.9")
    try:
        assert session.ctl.read_only and session.ctl._channel().read_only
        with pytest.raises(AmigentdError, match="AMIGENT_READ_ONLY"):
            session.screen.connect()
    finally:
        session.lookout._pool.shutdown(wait=True)


# ---------------------------------------------------------------- read only, at the MCP door

def test_server_door_shows_and_runs_only_the_allowlist(monkeypatch):
    monkeypatch.setattr(server, "READ_ONLY", True)
    shown = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert shown == server.READ_ONLY_TOOLS
    for name in ("amiga_exec", "amiga_brand_new_tool"):
        with pytest.raises(Exception, match="not offered"):
            asyncio.run(server.mcp.call_tool(name, {"command": "Version"}))
    # this process registered everything, so a read-only start here would be refused
    assert "tool amiga_exec" in server.read_only_problems()


def test_read_only_tasks_leave_out_the_powerpc_section(monkeypatch):
    monkeypatch.setattr(server, "READ_ONLY", True)
    monkeypatch.setattr(server, "_tasks", lambda: [])
    monkeypatch.setattr(server, "_ppc_task_section", lambda *a: pytest.fail("ran Version, CPU and stat"))
    server.amiga_tasks()


def test_read_only_server_skips_the_update_check(monkeypatch):
    asked, ran = [], []
    monkeypatch.setattr(server.update, "refresh_in_background", lambda *a, **k: asked.append(1))
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(server, "read_only_problems", lambda: [])
    monkeypatch.setattr(server, "READ_ONLY", True)
    server.main()
    assert ran == [1] and asked == []
    monkeypatch.setattr(server, "READ_ONLY", False)
    server.main()
    assert asked == [1]


def test_read_only_tools_are_real_tools():
    registered = {t.name for t in asyncio.run(server.FastMCP.list_tools(server.mcp))}
    assert server.READ_ONLY_TOOLS <= registered
    assert all(server.annotations_for(name) is None for name in registered)  # unrestricted here


def test_read_only_refuses_a_recursive_listing(monkeypatch):
    monkeypatch.setattr(server, "READ_ONLY", True)
    fn = getattr(server.amiga_list, "fn", server.amiga_list)
    with pytest.raises(server.AmigaError, match="recursive listing is not offered"):
        fn("SYS:", recursive=True)


def test_read_only_instructions_name_only_the_offered_tools():
    text = server.READ_ONLY_INSTRUCTIONS
    assert set(re.findall(r"\bamiga_\w+", text)) == server.READ_ONLY_TOOLS
    assert "enforces" in text
    lowered = text.lower()
    for absent in ("exec", "shell", "status", "write", "memory", "restart", "screenshot", "click",
                   "key", "mouse", "update", "power", "machines"):
        assert absent not in lowered, absent
    assert server.INSTRUCTIONS != text and "amiga_exec" in server.INSTRUCTIONS  # unrestricted here


# ---------------------------------------------------------------- the real server over stdio

class Stdio:
    """amigent-server as an MCP client runs it: one JSON-RPC message per line, nothing else."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen([sys.executable, "-m", "amigent.server"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.lines: queue.Queue = queue.Queue()
        self.out: list[bytes] = []
        self.err = b""
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        self.next = 0

    def _read(self):
        for line in self.proc.stdout:
            self.out.append(line)
            self.lines.put(line)

    def _read_err(self):
        self.err = self.proc.stderr.read()

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next += 1
        self.send({"jsonrpc": "2.0", "id": self.next, "method": method, "params": params or {}})
        while True:
            message = json.loads(self.lines.get(timeout=60))  # a stray print would fail here
            assert message["jsonrpc"] == "2.0"
            if message.get("id") == self.next:
                return message

    def start(self) -> dict:
        reply = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                            "clientInfo": {"name": "test", "version": "0"}})
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return reply

    def call(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def stop(self) -> bytes:
        self.proc.stdin.close()
        try:
            code = self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:  # mcp 1.6 and 1.7: the stdio server outlives its input
            self.proc.terminate()
            self.proc.wait(timeout=20)
        else:
            assert code == 0
        return b"".join(self.out) + self.err


def _server_env(tmp_path: Path, port: int, ctl_port: int, **switches) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AMIGENT_")}
    env.update(XDG_CONFIG_HOME=str(tmp_path / "xdg"), AMIGENT_HOST=HOST,
               AMIGENT_SCREEN_PORT=str(port), AMIGENT_CTL_PORT=str(ctl_port),
               PYTHONPATH=os.pathsep.join([str(SRC), env.get("PYTHONPATH", "")]), **switches)
    return env


def _child_config(tmp_path: Path, certificate: bytes | None) -> Path:
    folder = tmp_path / "xdg" / "amigent"
    folder.mkdir(parents=True)
    (folder / f"key-{HOST}").write_text(KEY + "\n")
    if certificate:
        (folder / f"tls-{HOST}.pem").write_bytes(certificate)
    return folder


def _hints(tool: dict) -> dict:
    return tool.get("annotations") or {}


def test_stdio_read_only_and_tls_only(stage, tmp_path):
    world = stage()
    config = _child_config(tmp_path, world["certificate"])
    mcp = Stdio(_server_env(tmp_path, world["port"], world["ctl"].port,
                            AMIGENT_READ_ONLY="1", AMIGENT_REQUIRE_TLS="1"))
    try:
        hello = mcp.start()["result"]
        assert hello["serverInfo"]["name"] == "amigent" and "tools" in hello["capabilities"]
        assert hello.get("instructions") == (server.READ_ONLY_INSTRUCTIONS if INSTRUCTED else None)
        tools = mcp.request("tools/list")["result"]["tools"]
        assert {t["name"] for t in tools} == server.READ_ONLY_TOOLS
        assert all(_hints(t).get("readOnlyHint") is (True if server.ANNOTATIONS else None) for t in tools)
        assert all("machine" in t["inputSchema"]["properties"] for t in tools)
        assert mcp.request("resources/list")["result"]["resources"] == []
        assert mcp.request("prompts/list")["result"]["prompts"] == []

        listed = mcp.call("amiga_list", {"path": "SYS:"})
        assert not listed.get("isError") and "C" in listed["content"][0]["text"]
        for name in ("amiga_screen_text", "amiga_tasks", "amiga_loaded", "amiga_notes", "amiga_installs"):
            assert not mcp.call(name, {}).get("isError"), name
        for name, arguments in FORBIDDEN.items():
            refused = mcp.call(name, arguments)
            assert refused.get("isError") and "not offered" in refused["content"][0]["text"], name
        aimed = mcp.call("amiga_exec", {"command": "Version", "machine": "A1"})
        assert aimed.get("isError") and "not offered" in aimed["content"][0]["text"]
    finally:
        said = mcp.stop()
    assert not (config / "update.json").exists(), "a read-only server looked for an update"
    assert set(world["fake"].counts) <= {CMD_CHANNEL, SYSINFO, DIR, TEXT, LOADED, TASKS, COMPRESS}
    assert world["fake"].files["SYS:S/Startup-Sequence"] == b"; boot\n"
    assert world["front"].handshakes >= 1 and set(world["fake"].keys_heard) == {KEY}
    _no_plain(world)
    assert KEY.encode() not in said, "the key turned up on stdout or stderr"
    for path in config.rglob("*"):
        if path.is_file() and path.name != f"key-{HOST}":
            assert KEY.encode() not in path.read_bytes(), path


def test_stdio_wrong_certificate_fails_closed(stage, tmp_path):
    world = stage(pin="other")
    _child_config(tmp_path, world["certificate"])
    mcp = Stdio(_server_env(tmp_path, world["port"], world["ctl"].port,
                            AMIGENT_READ_ONLY="1", AMIGENT_REQUIRE_TLS="1"))
    try:
        mcp.start()
        failed = mcp.call("amiga_list", {"path": "SYS:"})
        assert failed.get("isError")
    finally:
        said = mcp.stop()
    assert world["front"].accepted >= 1 and world["fake"].keys_heard == []
    _no_plain(world)
    assert KEY.encode() not in said


def test_stdio_unrestricted_keeps_every_tool(tmp_path):
    fake = FakeAmiga()
    fake.add("SYS:C/Dir", b"dir")
    ctl = Tripwire()
    _child_config(tmp_path, None)
    mcp = Stdio(_server_env(tmp_path, fake.port, ctl.port, AMIGENT_NO_UPDATE_CHECK="1"))
    try:
        hello = mcp.start()["result"]
        tools = {t["name"]: t for t in mcp.request("tools/list")["result"]["tools"]}
        listed = mcp.call("amiga_list", {"path": "SYS:"})
    finally:
        mcp.stop()
    assert set(tools) == {t.name for t in asyncio.run(server.FastMCP.list_tools(server.mcp))}
    assert len(tools) > len(server.READ_ONLY_TOOLS) and "amiga_exec" in tools
    assert hello.get("instructions") == (server.INSTRUCTIONS if INSTRUCTED else None)
    assert {"amiga_machines", "amiga_update", "amiga_pi_network", "amiga_emu68_update"} <= set(tools)
    for name, tool in tools.items():
        hints = _hints(tool)
        if not server.ANNOTATIONS:
            assert not hints, name  # mcp before 1.7 has no annotations to give
            continue
        assert not hints, name  # the default server makes no claim, as before
    assert not listed.get("isError") and "C" in listed["content"][0]["text"]
    assert ctl.knocks == 0
