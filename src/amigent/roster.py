"""Saved Amigas: stable identity, editable names and explicit address changes.

Old address lists are credential migration hints, never connection aliases.
Registry updates are serialized across threads and processes. The first load
of a legacy registry backs it up and copies credentials/history to ID paths;
original files are retained so migration is recoverable.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import threading
import uuid

from . import journal, locks, paths, states
from .errors import AmigaError

_lock = threading.RLock()
_local = threading.local()


class DirectAddress(str):
    """A one-off connection; never rediscover or rewrite a saved machine."""


class RegistryError(AmigaError, ValueError):
    pass


def list_file():
    return paths.FOLDER / "amigas.json"


@contextmanager
def transaction():
    with _lock:
        if getattr(_local, "active", False):
            yield
            return
        key = hashlib.sha256(str(paths.FOLDER.resolve()).encode()).hexdigest()[:20]
        with locks.exclusive(f"amigent-roster-{key}.lock"):
            _local.active = True
            try:
                yield
            finally:
                _local.active = False


def is_id(value: str) -> bool:
    return bool(re.fullmatch(r"amiga-[0-9a-f]{32}", value))


def validate_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 for c in name) or is_id(name):
        raise RegistryError("Use a name of 1 to 80 printable characters.")
    return name


def validate_address(host: str) -> str:
    host = host.strip()
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?", host) or is_id(host):
            raise RegistryError("Enter an IP address or hostname, not a saved Amiga's name.")
        return host.lower().rstrip(".")


def _address_key(host):
    """Compare literal addresses without DNS lookups or historical aliases."""
    try:
        return str(ipaddress.ip_address(host.strip()))
    except ValueError:
        return host.strip().rstrip(".").casefold()


def _copy_missing(source, target):
    if source.is_symlink() or not source.exists():
        return
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_missing(child, target / child.name)
    elif not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".migrating")
        shutil.copy2(source, tmp)
        os.replace(tmp, target)


def _all_stems(entry):
    return [entry["name"], *entry.get("previous_names", []), entry["host"], *entry.get("seen", [])]


def _shared_stems(entry, amigas):
    others = {paths.safe(v) for other in amigas if other is not entry for v in _all_stems(other)}
    return {paths.safe(v) for v in _all_stems(entry)} & others


def _blocked_pins(entry, amigas):
    stems = _shared_stems(entry, amigas) | set(entry.get("shared_legacy_stems", []))
    restored = {v for v in stems if (paths.FOLDER / f"tls-{v}.pem").exists()}
    return restored | set(entry.get("unclaimed_pins", []))


def _legacy_stems(entry, amigas):
    """Never borrow credentials from an address/name shared by two entries."""
    values = _all_stems(entry)
    for value in dict.fromkeys(values):
        safe = paths.safe(value)
        current = value in [entry["name"], *entry.get("previous_names", []), entry["host"]]
        if not any(other["id"] != entry["id"] and safe in {
            paths.safe(v) for v in [other["name"], *other.get("previous_names", []),
                                   other["host"], *(other.get("seen", []) if not current else [])]
        } for other in amigas):
            yield value


def _migrate_assets(entry, amigas, *, history=True):
    ident = entry["id"]
    blocked = _blocked_pins(entry, amigas)
    allowed = list(_legacy_stems(entry, amigas))
    for old in allowed:
        for prefix, suffix in (("key-", ""), ("tls-", ".pem")):
            if prefix == "tls-" and paths.safe(old) in blocked:
                continue
            _copy_missing(paths.FOLDER / f"{prefix}{paths.safe(old)}{suffix}",
                          paths.FOLDER / f"{prefix}{ident}{suffix}")
    if not history:
        return
    for name in [entry["name"], *entry.get("previous_names", [])]:
        # Name-derived history is only safe when its filename is unambiguous.
        if name not in allowed:
            continue
        for folder, suffix in (("notes", ".json"), ("installs", ""), ("inventory", "")):
            _copy_missing(paths.FOLDER / folder / (paths.safe(name) + suffix),
                          paths.FOLDER / folder / (ident + suffix))
        for filename in ("uptime.json", "link.json"):
            path = paths.FOLDER / filename
            if path.exists():
                values = json.loads(path.read_text(encoding="utf-8"))
                if ident not in values and name in values:
                    values[ident] = values[name]
                    _atomic_json(path, values)


def _migrate_v14(amigas, pending):
    # State labels and machine names can both contain hyphens. Only copy a
    # snapshot if all matching filename prefixes identify the same machine.
    store = states.Store(paths.FOLDER / "states")
    prefixes = {}
    owners = {}
    for entry in amigas:
        ident = entry["id"]
        for name in [ident, entry["name"], *entry.get("previous_names", [])]:
            owners.setdefault(journal._safe(name).casefold(), set()).add(ident)
            prefix = store.path(name, "").stem
            prefixes.setdefault(prefix, set()).add(ident)
    for source in store.folder.glob("*.json"):
        if not source.is_file():
            continue
        matches = [p for p in prefixes if source.name.casefold().startswith(p.casefold())]
        ids = {ident for prefix in matches for ident in prefixes[prefix]}
        if len(ids) == 1 and ids <= pending and len(matches) == 1:
            ident, = ids
            target = store.folder / (ident + "-" + source.name[len(matches[0]):])
            _copy_missing(source, target)
    for entry in amigas:
        ident = entry["id"]
        if ident not in pending:
            continue
        for name in [entry["name"], *entry.get("previous_names", [])]:
            stem = journal._safe(name)
            if stem in (".", "..") or owners[stem.casefold()] != {ident} or stem == ident:
                continue
            for folder, suffix in (("journal", ".jsonl"), ("screens", ""), ("checkpoints", "")):
                _copy_missing(paths.FOLDER / folder / (stem + suffix), paths.FOLDER / folder / (ident + suffix))
        entry["v14_history_migrated"] = True


def _legacy_id(name):
    return "amiga-" + uuid.uuid5(uuid.NAMESPACE_URL, str(paths.FOLDER.resolve()) + "\0" + name).hex


def _atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as out:
        json.dump(value, out, indent=1, ensure_ascii=False)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, path)


def load() -> list[dict]:
    with transaction():
        try:
            raw = list_file().read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as exc:
            raise RegistryError(f"Cannot read {list_file()}: {exc}") from exc
        try:
            amigas = json.loads(raw)
            if not isinstance(amigas, list) or any(
                not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a["name"]
                or not isinstance(a.get("host"), str) or not a["host"] for a in amigas
            ):
                raise ValueError("expected a list of named Amigas with addresses")
            ids = [a["id"] for a in amigas if "id" in a]
            if any(not isinstance(i, str) or not is_id(i) for i in ids) or len(set(ids)) != len(ids):
                raise ValueError("invalid or duplicate machine IDs")
            for a in amigas:
                for field in ("seen", "previous_names", "unclaimed_pins", "shared_legacy_stems"):
                    values = a.get(field, [])
                    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                        raise ValueError(f"{field} must be a list of strings")
        except (ValueError, TypeError) as e:
            raise RegistryError(f"Cannot read {list_file()}: {e}") from e
        backup = list_file().with_name("amigas.pre-id.json")
        if any("id" not in a for a in amigas):
            if not backup.exists():
                backup.write_text(raw, encoding="utf-8")
            for entry in amigas:
                # Deterministic until saved: a failed migration resumes the same copies.
                if "id" not in entry:
                    entry["legacy_credentials"] = True
                entry.setdefault("id", _legacy_id(entry["name"]))
            if len({a["id"] for a in amigas}) != len(amigas):
                raise RegistryError("Duplicate saved names must be resolved before migration.")
        pending = [a for a in amigas if a.get("legacy_credentials") and not a.get("history_migrated")]
        if pending:
            # The original roster identifies the common key's sole owner even
            # if this is an upgrade from an earlier ID migration.
            original = json.loads(backup.read_text()) if backup.exists() else []
            common_owner = _legacy_id(original[0]["name"]) if len(original) == 1 else None
            for entry in pending:
                shared = _shared_stems(entry, amigas)
                entry["shared_legacy_stems"] = sorted(shared)
                entry["unclaimed_pins"] = sorted(_blocked_pins(entry, amigas))
                _migrate_assets(entry, amigas)
                if entry["id"] == common_owner:
                    _copy_missing(paths.FOLDER / "key", paths.FOLDER / f"key-{common_owner}")
                entry["history_migrated"] = True
        v14 = {a["id"] for a in amigas if a.get("legacy_credentials") and not a.get("v14_history_migrated")}
        if v14:
            _migrate_v14(amigas, v14)
        if pending or v14:
            save(amigas)
        return amigas


def save(amigas: list[dict]) -> None:
    with transaction():
        _atomic_json(list_file(), amigas)


def _find(host, amigas):
    if not host:
        return None
    # Exact current names/IDs precede old display names. Addresses never use seen[].
    for matches in (
        [a for a in amigas if host == a["id"] or host.casefold() == a["name"].casefold()],
        [a for a in amigas if _address_key(host) == _address_key(a["host"])],
        [a for a in amigas if host.casefold() in [n.casefold() for n in a.get("previous_names", [])]],
    ):
        if len(matches) > 1:
            raise RegistryError(f"Ambiguous Amiga {host!r}; edit the duplicate addresses in Your Amigas.")
        if matches:
            return matches[0]
    return None


def find(host: str) -> dict | None:
    if not host or isinstance(host, DirectAddress):
        return None
    return _find(host, load())


def identity(host: str) -> str:
    entry = find(host)
    return entry["id"] if entry else host


def default() -> str:
    amigas = load()
    return next((a["name"] for a in amigas if a.get("default")),
                amigas[0]["name"] if len(amigas) == 1 else "")


def pick(host: str = "") -> str:
    return host or os.environ.get("AMIGENT_HOST", "") or default()


def name_of(host: str) -> str:
    entry = find(host)
    return entry["name"] if entry else host


def conflicts(entry: dict, amigas: list[dict] | None = None) -> list[str]:
    return [a["name"] for a in (load() if amigas is None else amigas)
            if a["id"] != entry["id"] and _address_key(a["host"]) == _address_key(entry["host"])]


def address(host: str) -> str:
    entry = find(host)
    if entry:
        others = conflicts(entry)
        if others:
            raise RegistryError(f"{entry['host']} is also assigned to {', '.join(others)}. Edit its address in Your Amigas.")
        return entry["host"]
    if is_id(host):
        raise RegistryError("This Amiga was removed. Select another machine in Your Amigas.")
    return host


def pin_conflicts(host: str) -> tuple[str, set[str]]:
    """Pins with multiple possible owners, including pins restored after migration."""
    amigas = load()
    entry = _find(str(host), amigas)
    if not entry or not entry.get("legacy_credentials"):
        return "", set()
    return entry["id"], _blocked_pins(entry, amigas)


def stems(host: str) -> list[str]:
    amigas = load()
    entry = _find(str(host), amigas)
    if entry:
        return [entry["id"], *(_legacy_stems(entry, amigas) if entry.get("legacy_credentials") else [])]
    return [host]


def aliases(host: str) -> list[str]:
    names = stems(host)
    if not find(str(host)):
        try:
            names.append(socket.gethostbyname(host))
        except OSError:
            pass
    return list(dict.fromkeys(names))


def unique_name(name: str, amigas: list[dict]) -> str:
    taken = {a["name"].casefold() for a in amigas}
    if name.casefold() not in taken:
        return name
    number = 2
    while f"{name} {number}".casefold() in taken:
        number += 1
    return f"{name} {number}"


def _check_address(entry, host, amigas):
    if any(a is not entry and _address_key(a["host"]) == _address_key(host) for a in amigas):
        raise RegistryError(f"{host} is already assigned to another Amiga. Select that machine or use a different address.")


def remember(name: str, host: str, make_default: bool = False) -> dict:
    """Register a machine; an existing name cannot silently change addresses."""
    name, host = validate_name(name), validate_address(host)
    with transaction():
        amigas = load()
        entry = next((a for a in amigas if a["name"].casefold() == name.casefold()), None)
        if entry and _address_key(entry["host"]) != _address_key(host):
            raise RegistryError(f"{name!r} is already registered at {entry['host']}. Use Edit address to change it.")
        if entry is None and any(name.casefold() in [n.casefold() for n in a.get("previous_names", [])] for a in amigas):
            raise RegistryError("That name is an alias of an existing Amiga. Choose another name.")
        _check_address(entry, host, amigas)
        if entry is None:
            entry = {"id": "amiga-" + uuid.uuid4().hex, "name": name, "host": host, "seen": [host]}
            amigas.append(entry)
        if make_default or len(amigas) == 1:
            for a in amigas:
                a.pop("default", None)
            entry["default"] = True
        # Setup may have saved credentials by address before registration.
        # Never adopt a removed machine's name-based history for a new ID.
        for prefix, suffix in (("key-", ""), ("tls-", ".pem")):
            if prefix == "tls-" and paths.safe(host) in _blocked_pins(entry, amigas):
                continue
            _copy_missing(paths.FOLDER / f"{prefix}{paths.safe(host)}{suffix}",
                          paths.FOLDER / f"{prefix}{entry['id']}{suffix}")
        save(amigas)
        return entry


def edit(host: str, *, name: str | None = None, address: str | None = None,
         make_default: bool = False) -> dict:
    with transaction():
        amigas = load()
        entry = _find(host, amigas)
        if not entry:
            raise RegistryError("No such saved Amiga.")
        if name is not None:
            name = validate_name(name)
            if any(a is not entry and (name.casefold() == a["name"].casefold() or
                   name.casefold() in [n.casefold() for n in a.get("previous_names", [])]) for a in amigas):
                raise RegistryError("That name already belongs to another Amiga.")
        if address is not None:
            address = validate_address(address)
            _check_address(entry, address, amigas)
        if entry.get("legacy_credentials"):
            _migrate_assets(entry, amigas, history=False)
        if name is not None and name != entry["name"]:
            entry.setdefault("previous_names", []).append(entry["name"])
            entry["name"] = name
        if address is not None:
            entry["seen"] = list(dict.fromkeys([*entry.get("seen", []), entry["host"], address]))
            entry["host"] = address
        if make_default:
            for a in amigas:
                a.pop("default", None)
            entry["default"] = True
        save(amigas)
        return entry


def rename(old: str, new: str) -> bool:
    if not find(old):
        return False
    edit(old, name=new)
    return True


def forget(name: str) -> bool:
    with transaction():
        amigas = load()
        entry = _find(name, amigas)
        if entry is None:
            return False
        kept = [a for a in amigas if a["id"] != entry["id"]]
        if kept and not any(a.get("default") for a in kept):
            kept[0]["default"] = True
        save(kept)
        return True
