"""The roll call: which Amiga is which, by name and every address it ever wore.

~/.config/amigent/amigas.json, read by everybody, written by setup, fleet and
the search in machines.py:

    [{"name": "A1200", "host": "192.168.1.50", "seen": ["192.168.1.50"], "default": true}]

Every client takes a name or an address; a name means its last known address.
Key and certificate files are looked up under aliases(): the name, every
address the Amiga ever had, and what DNS says, so installs from the pre-name
era keep working. Imports nothing above paths.py, so secret, tls and machines
can all lean on it without a cycle in sight.
"""

from __future__ import annotations

import json
import os
import socket
import threading

from . import paths

_lock = threading.RLock()


def list_file():
    return paths.FOLDER / "amigas.json"


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


def aliases(host: str) -> list[str]:
    """stems(), plus whatever DNS makes of host: the full line-up for a file lookup."""
    names = stems(host) or [host]
    try:
        names.append(socket.gethostbyname(host))
    except OSError:
        pass
    return names


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


def rename(old: str, new: str) -> bool:
    with _lock:
        amigas = load()
        entry = next((a for a in amigas if a["name"].lower() == old.lower()), None)
        if entry is None or any(a["name"].lower() == new.lower() for a in amigas if a is not entry):
            return False
        for stem in ("key-", "tls-"):  # key and cert move house with the name
            suffix = ".pem" if stem == "tls-" else ""
            path = paths.FOLDER / f"{stem}{paths.safe(entry['name'])}{suffix}"
            if path.exists():
                path.rename(paths.FOLDER / f"{stem}{paths.safe(new)}{suffix}")
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
