"""Two questions for the wire: who am I on this LAN, and does anybody answer over there."""

from __future__ import annotations

import socket

PORT = 6801  # amigentd; amigactld sits one below, TLS one above


def lan_address(peer: str = "", port: int = PORT, timeout: float = 5) -> str:
    """This PC's address: as peer sees it over TCP (NAT may disagree with the LAN), else the
    one the default route leaves from. Raises OSError when there is no answer to either."""
    if peer:
        with socket.create_connection((peer, port), timeout) as s:
            return s.getsockname()[0]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # not a single byte leaves; UDP connect just picks the interface
        return s.getsockname()[0]


def greet(host: str, port: int = PORT, timeout: float = 2.0,
          connect_timeout: float | None = None) -> bytes:
    """Knock and listen: the first bytes (up to 8) a daemon says on connect; b"" for silence."""
    try:
        with socket.create_connection((host, port), connect_timeout or timeout) as s:
            s.settimeout(timeout)
            return s.recv(8)
    except OSError:
        return b""
