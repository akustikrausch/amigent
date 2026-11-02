"""The shared key between the PC and amigentd.

amigent-setup creates one per Amiga, puts it there as S:amigent.key and keeps a
copy in ~/.config/amigent/key-<address> (found again under the Amiga's name,
see machines.py). The first one also goes to
~/.config/amigent/key, which serves any address without a key of its own.
AMIGENT_KEY overrides the files.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
from pathlib import Path

FOLDER = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "amigent"


def safe(host: str) -> str:
    """host as part of a file name: IPv6 colons and path characters become _."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host).strip(".") or "_"


def _own(host: str) -> Path | None:
    """The key file of this Amiga: under its name, one of its addresses, or the address a
    host name resolves to."""
    from . import machines
    names = machines.stems(host)
    try:
        names.append(socket.gethostbyname(host))
    except OSError:
        pass
    for name in names:
        path = FOLDER / f"key-{safe(name)}"
        if path.exists():
            return path
    return None


def key_file(host: str = "") -> Path:
    """The key for this host: its own, the one kept under one of its addresses, or the common key.

    The common key belongs to the first Amiga set up. Once there are keys for
    several Amigas it is only used for a host that resolves to none of them.
    """
    return (_own(host) if host else None) or FOLDER / "key"


def load_key(host: str = "") -> str | None:
    if os.environ.get("AMIGENT_KEY"):
        return os.environ["AMIGENT_KEY"].strip()
    path = key_file(host)
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def own_key(host: str) -> str | None:
    """The key kept for exactly this host, ignoring the common one."""
    path = _own(host)
    try:
        return path.read_text().strip() or None if path else None
    except OSError:
        return None


def _write(path: Path, key: str) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as out:
        out.write(key + "\n")
    os.chmod(path, 0o600)  # an older file keeps its mode otherwise


def save_key(key: str, host: str) -> Path:
    FOLDER.mkdir(parents=True, exist_ok=True)
    path = FOLDER / f"key-{safe(host)}"
    _write(path, key)
    if not (FOLDER / "key").exists():
        _write(FOLDER / "key", key)
    return path


def new_key() -> str:
    return secrets.token_urlsafe(24)
