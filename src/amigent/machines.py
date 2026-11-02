"""Which Amiga is which: names, addresses, and tracking one down after DHCP moved it.

~/.config/amigent/amigas.json is the roll call (the fleet reads it too):

    [{"name": "A1200", "host": "192.168.1.50", "seen": ["192.168.1.50"], "default": true}]

Every client takes a name or an address; a name means its last known address.
Key and certificate are looked up under the name and every address the Amiga
ever had (key-<name>, key-<address>, tls-<name>.pem, tls-<address>.pem), so
installs from the pre-name era keep working.

When the address goes silent, the local network is swept for amigentd and only
the Amiga that proves it is ours gets picked: its pinned certificate must match,
or, without TLS, it must accept our key. A neighbour's Amiga on the same wire is
never mistaken for yours. The new address is remembered; an address nobody knew
yet is filed under the name "Amiga" the first time it is found this way.
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

from . import net, secret

GREETING_PORT = 6801
SEARCH_EVERY = 30.0  # seconds between two sweeps for the same Amiga; a /24 scan is no free lunch
_lock = threading.RLock()
_searched: dict[str, float] = {}
_busy = threading.local()


# ---------------------------------------------------------------- the list

def list_file():
    return secret.FOLDER / "amigas.json"


def load() -> list[dict]:
    try:
        amigas = json.loads(list_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [a for a in amigas if isinstance(a, dict) and a.get("name") and a.get("host")]


def save(amigas: list[dict]) -> None:
    path = list_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(amigas, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def find(host: str) -> dict | None:
    """The entry for a name, or for any address an Amiga ever answered at."""
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
    """The Amiga to use when nobody says: the marked one, or the only one."""
    amigas = load()
    for amiga in amigas:
        if amiga.get("default"):
            return amiga["name"]
    return amigas[0]["name"] if len(amigas) == 1 else ""


def pick(host: str = "") -> str:
    """Who to dial: the given name or address, else AMIGENT_HOST, else the default."""
    return host or os.environ.get("AMIGENT_HOST", "") or default()


def name_of(host: str) -> str:
    entry = find(host)
    return entry["name"] if entry else host


def address(host: str) -> str:
    """The address to dial for a name or address."""
    entry = find(host)
    return entry["host"] if entry else host


def stems(host: str) -> list[str]:
    """Name tags this Amiga's key and certificate files may wear, best first."""
    entry = find(host)
    if not entry:
        return [host] if host else []
    names = [entry["name"], entry["host"], *entry.get("seen", [])]
    return list(dict.fromkeys(n for n in names if n))


def unique_name(name: str, amigas: list[dict]) -> str:
    """name, or name 2, name 3... whichever the roll call has not handed out yet."""
    taken = {a["name"].lower() for a in amigas}
    if name.lower() not in taken:
        return name
    number = 2
    while f"{name} {number}".lower() in taken:
        number += 1
    return f"{name} {number}"


def remember(name: str, host: str, make_default: bool = False) -> dict:
    """Sign up a new Amiga, or hand a known one its new address."""
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


def rename(old: str, new: str) -> bool:
    with _lock:
        amigas = load()
        entry = next((a for a in amigas if a["name"].lower() == old.lower()), None)
        if entry is None or any(a["name"].lower() == new.lower() for a in amigas if a is not entry):
            return False
        for stem in ("key-", "tls-"):  # key and cert move house with the name
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
        mine = net.lan_address()
    except OSError:
        return []
    return [ipaddress.ip_network(f"{mine}/24", strict=False)]


def _greets(host: str) -> bool:
    return net.greet(host, GREETING_PORT, 2, connect_timeout=1)[:4] == b"AMGT"


def scan() -> list[str]:
    """Every address on the local /24 where amigentd says AMGT; 64 knocks at a time."""
    hosts = [str(h) for net in local_networks() for h in net.hosts()]
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        return [h for h, ok in zip(hosts, pool.map(_greets, hosts)) if ok]


def _pin(host: str):
    from . import tls
    path = tls.pinned(host)
    return path if path.exists() else None


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
        pin, key = _pin(host), secret.load_key(host)
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
