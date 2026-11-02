"""Which Amiga is which: names, addresses, and finding one again after its address changed.

~/.config/amigent/amigas.json lists the known Amigas (it is also the fleet's list):

    [{"name": "A1200", "host": "192.168.1.50", "seen": ["192.168.1.50"], "default": true}]

Every client takes a name or an address. A name stands for its last known
address. Key and certificate are found under the name and under every address
the Amiga had (key-<name>, key-<address>, tls-<name>.pem, tls-<address>.pem),
so installations from before names keep working.

When the address stops answering, the local network is searched for amigentd
and the Amiga that proves to be this one is taken: its pinned certificate must
match, or, without TLS, its key must be accepted. A stranger's Amiga on the same
network is never mistaken for yours. The new address is remembered. An address
that nobody knows yet is added under the name "Amiga" the first time it is
found again this way.
"""

from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import socket
import ssl
import struct
import threading
import time

from . import secret

GREETING_PORT = 6801
SEARCH_EVERY = 30.0  # seconds between two searches for the same Amiga
_lock = threading.RLock()
_searched: dict[str, float] = {}
_busy = threading.local()


# ---------------------------------------------------------------- the list

def list_file():
    return secret.FOLDER / "amigas.json"


def load() -> list[dict]:
    try:
        amigas = json.loads(list_file().read_text())
    except (OSError, ValueError):
        return []
    return [a for a in amigas if isinstance(a, dict) and a.get("name") and a.get("host")]


def save(amigas: list[dict]) -> None:
    path = list_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(amigas, indent=1))
    os.replace(tmp, path)


def find(host: str) -> dict | None:
    """The entry for a name or for one of the addresses an Amiga had."""
    if not host:
        return None
    amigas = load()
    for amiga in amigas:
        if amiga["name"].lower() == host.lower():
            return amiga
    for amiga in amigas:
        if host == amiga["host"] or host in amiga.get("seen", []):
            return amiga
    return None


def default() -> str:
    """The name of the Amiga to use when none is given: the marked one, or the only one."""
    amigas = load()
    for amiga in amigas:
        if amiga.get("default"):
            return amiga["name"]
    return amigas[0]["name"] if len(amigas) == 1 else ""


def pick(host: str = "") -> str:
    """What to connect to: the given name or address, else AMIGENT_HOST, else the default."""
    return host or os.environ.get("AMIGENT_HOST", "") or default()


def name_of(host: str) -> str:
    entry = find(host)
    return entry["name"] if entry else host


def address(host: str) -> str:
    """The address to dial for a name or address."""
    entry = find(host)
    return entry["host"] if entry else host


def stems(host: str) -> list[str]:
    """Names the key and certificate files of this Amiga may carry, best first."""
    entry = find(host)
    if not entry:
        return [host] if host else []
    names = [entry["name"], entry["host"], *entry.get("seen", [])]
    return list(dict.fromkeys(n for n in names if n))


def _unique(name: str, amigas: list[dict]) -> str:
    taken = {a["name"].lower() for a in amigas}
    if name.lower() not in taken:
        return name
    number = 2
    while f"{name} {number}".lower() in taken:
        number += 1
    return f"{name} {number}"


def remember(name: str, host: str, make_default: bool = False) -> dict:
    """Add an Amiga or give a known one its new address."""
    with _lock:
        amigas = load()
        entry = next((a for a in amigas if a["name"].lower() == name.lower()), None)
        if entry is None:
            entry = {"name": name, "host": host, "seen": []}
            amigas.append(entry)
        old = entry["host"]
        entry["host"] = host
        seen = entry.setdefault("seen", [])
        for value in (old, host):
            if value and value not in seen:
                seen.append(value)
        if make_default or len(amigas) == 1:
            for amiga in amigas:
                amiga.pop("default", None)
            entry["default"] = True
        save(amigas)
        return entry


def move(host: str, new: str) -> dict:
    """The Amiga known as host now answers at new."""
    entry = find(host)
    if not entry:  # an address from before names: keep it as an alias, so its files still apply
        entry = remember(_unique("Amiga", load()), host)
    with _lock:
        _searched.pop(host, None)
    return remember(entry["name"], new)


def rename(old: str, new: str) -> bool:
    with _lock:
        amigas = load()
        entry = next((a for a in amigas if a["name"].lower() == old.lower()), None)
        if entry is None or any(a["name"].lower() == new.lower() for a in amigas if a is not entry):
            return False
        for stem in ("key-", "tls-"):  # files under the old name follow it
            suffix = ".pem" if stem == "tls-" else ""
            path = secret.FOLDER / f"{stem}{secret.safe(entry['name'])}{suffix}"
            if path.exists():
                path.rename(secret.FOLDER / f"{stem}{secret.safe(new)}{suffix}")
        entry["name"] = new
        save(amigas)
        return True


def forget(name: str) -> bool:
    with _lock:
        amigas = load()
        kept = [a for a in amigas if a["name"].lower() != name.lower()]
        if len(kept) == len(amigas):
            return False
        if kept and not any(a.get("default") for a in kept):
            kept[0]["default"] = True
        save(kept)
        return True


# ---------------------------------------------------------------- searching

def local_networks() -> list[ipaddress.IPv4Network]:
    """The /24 this PC sits in (the one its default route leaves from)."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("192.0.2.1", 9))
        mine = probe.getsockname()[0]
        probe.close()
    except OSError:
        return []
    return [ipaddress.ip_network(f"{mine}/24", strict=False)]


def _greets(host: str) -> bool:
    try:
        with socket.create_connection((host, GREETING_PORT), 1) as s:
            s.settimeout(2)
            return s.recv(4) == b"AMGT"
    except OSError:
        return False


def scan() -> list[str]:
    """Addresses on the local network where amigentd answers."""
    hosts = [str(h) for net in local_networks() for h in net.hosts()]
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        return [h for h, ok in zip(hosts, pool.map(_greets, hosts)) if ok]


def _pin(host: str):
    from . import tls
    path = tls.pinned(host)
    return path if path.exists() else None


def _key(host: str) -> str | None:
    from .secret import load_key
    return load_key(host)


def _proves_certificate(candidate: str, pin) -> bool:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(pin))
    try:
        with socket.create_connection((candidate, GREETING_PORT + 1), 3) as raw:
            with context.wrap_socket(raw) as s:
                s.settimeout(3)
                return s.recv(4) == b"AMGT"
    except (OSError, ssl.SSLError):
        return False


def _proves_key(candidate: str, key: str) -> bool:
    """amigentd accepts this key (command 17, see docs/PROTOCOL.md)."""
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
                return False  # no key asked for, so nothing is proven
            raw = key.encode()
            s.sendall(struct.pack(">BBHHHH", 17, 0, len(raw), 0, 0, 0) + raw)
            return s.recv(1) == b"\0"
    except OSError:
        return False


def relocate(host: str) -> str | None:
    """Search the network for this Amiga; returns and remembers its new address, if found."""
    if getattr(_busy, "on", False) or not host:
        return None
    now = time.monotonic()
    with _lock:
        if now - _searched.get(host, -SEARCH_EVERY) < SEARCH_EVERY:
            return None
        _searched[host] = now
    _busy.on = True
    try:
        current = address(host)
        pin, key = _pin(host), _key(host)
        found = [h for h in scan() if h != current]
        if pin:
            match = next((h for h in found if _proves_certificate(h, pin)), None)
        elif key:
            match = next((h for h in found if _proves_key(h, key)), None)
        else:
            match = found[0] if len(found) == 1 else None
        if match:
            move(host, match)
        return match
    finally:
        _busy.on = False
