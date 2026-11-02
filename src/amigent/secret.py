"""The handshake secret between PC and amigentd. No key, no party.

amigent-setup rolls one per Amiga, drops it there as S:amigent.key and keeps a
copy in ~/.config/amigent/key-<address> (also found under the Amiga's name,
see machines.py). The first one doubles as ~/.config/amigent/key, the
fallback for any address without a key of its own. AMIGENT_KEY trumps the files.
"""

from __future__ import annotations

import os
import re
import secrets
import socket
from pathlib import Path

FOLDER = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "amigent"


def safe(host: str) -> str:
    """host squeezed into a file name: IPv6 colons and path characters become _."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host).strip(".") or "_"


def _own(host: str) -> Path | None:
    """This Amiga's own key file: by name, by any address it ever had, or by what DNS says."""
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
    """The key for this host: its own, one filed under an old address, or the common key.

    The common key belongs to the first Amiga set up. With several Amigas on
    file it only covers a host that matches none of them.
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
    """The key filed for exactly this host; the common one doesn't count."""
    path = _own(host)
    try:
        return path.read_text().strip() or None if path else None
    except OSError:
        return None


def _write(path: Path, key: str) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as out:
        out.write(key + "\n")
    os.chmod(path, 0o600)  # O_CREAT's mode only bites on new files; old ones get 0600 here


def save_key(key: str, host: str) -> Path:
    FOLDER.mkdir(parents=True, exist_ok=True)
    path = FOLDER / f"key-{safe(host)}"
    _write(path, key)
    if not (FOLDER / "key").exists():
        _write(FOLDER / "key", key)
    return path


def new_key() -> str:
    return secrets.token_urlsafe(24)
