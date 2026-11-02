"""The shared key between the PC and amigentd.

amigent-setup creates it, puts it on the Amiga as S:amigent.key and keeps a
copy in ~/.config/amigent/key. AMIGENT_KEY overrides the file.
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


def save_key(key: str) -> Path:
    FOLDER.mkdir(parents=True, exist_ok=True)
    path = FOLDER / "key"
    path.write_text(key + "\n")
    path.chmod(0o600)
    return path


def new_key() -> str:
    return secrets.token_urlsafe(24)
