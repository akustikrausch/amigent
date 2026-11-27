"""Two opt-in switches for a server that should do less: AMIGENT_REQUIRE_TLS and AMIGENT_READ_ONLY.

Unset, empty, 0, false, no or off is off; any other value is on, so a typo in a
switch that adds safety errs on the safe side. Read on every call, never cached.
A leaf module: tls, channel, amigactl, power and machines all ask it.
"""

from __future__ import annotations

import os

OFF = ("", "0", "false", "no", "off")


def _on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in OFF


def require_tls() -> bool:
    """Every connection to amigentd goes over TLS with the pinned certificate, or not at all."""
    return _on("AMIGENT_REQUIRE_TLS")


def read_only() -> bool:
    """The MCP server offers only tools that change nothing, and the channel refuses the rest."""
    return _on("AMIGENT_READ_ONLY")
