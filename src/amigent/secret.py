"""The shared key between the PC and amigentd.

amigent-setup creates one per Amiga, puts it there as S:amigent.key and keeps a
copy in ~/.config/amigent/key-<address>. The first one also goes to
~/.config/amigent/key, which serves any address without a key of its own.
AMIGENT_KEY overrides the files.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

FOLDER = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "amigent"


def key_file(host: str = "") -> Path:
    """A key for one host if there is one, else the common key."""
    if host:
        own = FOLDER / f"key-{host}"
        if own.exists():
            return own
    return FOLDER / "key"


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
    try:
        return (FOLDER / f"key-{host}").read_text().strip() or None
    except OSError:
        return None


def _write(path: Path, key: str) -> None:
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as out:
        out.write(key + "\n")
    os.chmod(path, 0o600)  # an older file keeps its mode otherwise


def save_key(key: str, host: str) -> Path:
    FOLDER.mkdir(parents=True, exist_ok=True)
    path = FOLDER / f"key-{host}"
    _write(path, key)
    if not (FOLDER / "key").exists():
        _write(FOLDER / "key", key)
    return path


def new_key() -> str:
    return secrets.token_urlsafe(24)
