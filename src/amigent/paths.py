"""Where things live: the config folder on the PC, the Amiga half inside the package.

A leaf module on purpose: everybody imports it, it imports nobody.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

FOLDER = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "amigent"
AMIGA = Path(__file__).resolve().parent / "amiga"  # the Amiga half rides inside the package


def safe(host: str) -> str:
    """host squeezed into a file name: IPv6 colons and path characters become _."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host).strip(".") or "_"
