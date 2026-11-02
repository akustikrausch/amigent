"""Tracking an Amiga down after DHCP moved it; the roll call itself lives in roster.py.

When the address goes silent, the local network is swept for amigentd and only
the Amiga that proves it is ours gets picked: its pinned certificate must match,
or, without TLS, it must accept our key. A neighbour's Amiga on the same wire is
never mistaken for yours. The new address is remembered; an address nobody knew
yet is filed under the name "Amiga" the first time it is found this way.

The roster names are re-exported here, so machines.find & co. keep working.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import socket
import struct
import threading
import time

from . import net, secret, tls
from .roster import (address, aliases, default, find, forget, list_file, load, name_of,  # noqa: F401
                     pick, remember, rename, save, stems, unique_name)

GREETING_PORT = net.PORT
SEARCH_EVERY = 30.0  # seconds between two sweeps for the same Amiga; a /24 scan is no free lunch
_lock = threading.Lock()
_searched: dict[str, float] = {}
_busy = threading.local()


def move(host: str, new: str) -> dict:
    """The Amiga known as host now lives at new."""
    entry = find(host)
    if not entry:  # pre-name address: keep it as an alias so its key/cert files still match
        entry = remember(unique_name("Amiga", load()), host)
    forget_search(host)
    return remember(entry["name"], new)


def forget_search(host: str) -> None:
    """Lift the cool-down: the next relocate(host) sweeps at once."""
    with _lock:
        _searched.pop(host, None)


# ---------------------------------------------------------------- searching

def local_networks() -> list[ipaddress.IPv4Network]:
    """The /24 this PC sits in (the one its default route leaves from)."""
    try:
        mine = net.lan_address()
    except OSError:
        return []
    return [ipaddress.ip_network(f"{mine}/24", strict=False)]


def _greets(host: str) -> bool:
    return net.greet(host, GREETING_PORT, 2, connect_timeout=1)[:4] == b"AMGT"


def scan() -> list[str]:
    """Every address on the local /24 where amigentd says AMGT; 64 knocks at a time."""
    hosts = [str(h) for lan in local_networks() for h in lan.hosts()]
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        return [h for h, ok in zip(hosts, pool.map(_greets, hosts)) if ok]


def _proves_certificate(candidate: str, host: str) -> bool:
    """candidate shows host's pinned certificate; tls.wrap is the bouncer, as on every dial."""
    try:
        with socket.create_connection((candidate, GREETING_PORT + 1), 3) as raw:
            with tls.wrap(raw, host) as s:
                s.settimeout(3)
                return s.recv(4) == b"AMGT"
    except OSError:  # ssl.SSLError included, it is an OSError too
        return False


def _proves_key(candidate: str, key: str) -> bool:
    """amigentd swallows this key (command 17, see docs/PROTOCOL.md)."""
    try:
        with socket.create_connection((candidate, GREETING_PORT), 3) as s:
            s.settimeout(3)
            hello = b""
            while len(hello) < 8:
                chunk = s.recv(8 - len(hello))
                if not chunk:
                    return False
                hello += chunk
            if hello[:4] != b"AMGT":
                return False
            if not struct.unpack(">H", hello[6:8])[0] & 1:
                return False  # no key asked for: any Amiga would pass, so this proves nothing
            raw = key.encode()
            s.sendall(struct.pack(">BBHHHH", 17, 0, len(raw), 0, 0, 0) + raw)
            return s.recv(1) == b"\0"
    except OSError:
        return False


def relocate(host: str) -> str | None:
    """Sweep the network for this Amiga; returns and remembers its new address, if any."""
    if getattr(_busy, "on", False) or not host:  # this thread is mid-sweep already: no nesting
        return None
    now = time.monotonic()
    with _lock:
        if now - _searched.get(host, -SEARCH_EVERY) < SEARCH_EVERY:
            return None
        _searched[host] = now
    _busy.on = True
    try:
        current = address(host)
        pinned, key = tls.pinned(host).exists(), secret.load_key(host)
        found = [h for h in scan() if h != current]
        if pinned:
            match = next((h for h in found if _proves_certificate(h, host)), None)
        elif key:
            match = next((h for h in found if _proves_key(h, key)), None)
        else:
            match = found[0] if len(found) == 1 else None
        if match:
            move(host, match)
        return match
    finally:
        _busy.on = False
