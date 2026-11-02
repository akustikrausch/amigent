"""Optional TLS to amigentd, with the Amiga's certificate pinned on the PC.

amigent-setup creates a small EC certificate (fast enough for a 68k), puts
certificate and key into S:amigent-tls.pem on the Amiga and keeps the
certificate in ~/.config/amigent/tls-<host>.pem. With that file present the
clients talk TLS on the port after amigentd's; AMIGENT_TLS=off turns it off.
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path

from .secret import FOLDER


def pinned(host: str) -> Path:
    return FOLDER / f"tls-{host}.pem"


def wanted(host: str) -> bool:
    return os.environ.get("AMIGENT_TLS", "auto").lower() != "off" and pinned(host).exists()


def wrap(sock: socket.socket, host: str) -> ssl.SSLSocket:
    """TLS over sock, accepting exactly the certificate pinned for host."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # the pin is the identity; Amigas have no DNS names
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(pinned(host)))
    return context.wrap_socket(sock)


def connect(host: str, port: int, timeout: float) -> socket.socket:
    """A plain connection to port, or TLS to port + 1 when a certificate is pinned."""
    if wanted(host):
        sock = socket.create_connection((host, port + 1), timeout)
        try:
            return wrap(sock, host)
        except (OSError, ssl.SSLError):
            sock.close()
            raise
    return socket.create_connection((host, port), timeout)


def make_certificate(host: str) -> tuple[bytes, bytes] | None:
    """(certificate + key for the Amiga, certificate for the PC), or None without openssl."""
    tool = shutil.which("openssl")
    if not tool:
        return None
    with tempfile.TemporaryDirectory() as folder:
        key, cert = Path(folder) / "key.pem", Path(folder) / "cert.pem"
        subprocess.run([tool, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                        "-nodes", "-days", "7300", "-subj", f"/CN=amigent {host}",
                        "-keyout", str(key), "-out", str(cert)],
                       check=True, capture_output=True)
        certificate = cert.read_bytes()
        return certificate + key.read_bytes(), certificate


def remember(host: str, certificate: bytes) -> Path:
    FOLDER.mkdir(parents=True, exist_ok=True)
    path = pinned(host)
    path.write_bytes(certificate)
    return path
