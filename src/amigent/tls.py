"""Optional TLS to amigentd, the Amiga's cert pinned like a dongle: no CA, no DNS, no mercy.

amigent-setup cooks a small EC certificate (a 68k can chew that), puts
certificate and key into S:amigent-tls.pem on the Amiga and keeps the
certificate in ~/.config/amigent/tls-<host>.pem. With that file present the
clients speak TLS on the port after amigentd's; AMIGENT_TLS=off kills it.
The pin doubles as the Amiga's fingerprint when it turns up under a new
address (see machines.py).
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
from pathlib import Path

from .secret import FOLDER, safe


def pinned(host: str) -> Path:
    """This Amiga's pinned cert: by name, by any address it ever had, or by what DNS says."""
    from . import machines
    names = machines.stems(host) or [host]
    try:
        names = [*names, socket.gethostbyname(host)]
    except OSError:
        pass
    for name in names:
        path = FOLDER / f"tls-{safe(name)}.pem"
        if path.exists():
            return path
    return FOLDER / f"tls-{safe(names[0])}.pem"


def wanted(host: str) -> bool:
    return os.environ.get("AMIGENT_TLS", "auto").lower() != "off" and pinned(host).exists()


def wrap(sock: socket.socket, host: str) -> ssl.SSLSocket:
    """TLS over sock; the pinned certificate gets in, nobody else."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # the pin is the identity; Amigas don't do DNS names
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(pinned(host)))
    return context.wrap_socket(sock)


def _dial(address: str, host: str, port: int, timeout: float) -> socket.socket:
    if wanted(host):
        sock = socket.create_connection((address, port + 1), timeout)
        try:
            return wrap(sock, host)
        except (OSError, ssl.SSLError):
            sock.close()
            raise
    return socket.create_connection((address, port), timeout)


def connect(host: str, port: int, timeout: float) -> socket.socket:
    """Plain to port, or TLS to port + 1 when a certificate is pinned.

    host is a name or an address. No answer there? One sweep of the local
    network, and the Amiga is picked up again at its new address.
    """
    from . import machines
    address = machines.address(host)
    try:
        return _dial(address, host, port, timeout)
    except (OSError, ssl.SSLError):
        moved = machines.relocate(host)
        if not moved or moved == address:
            raise
        return _dial(moved, host, port, timeout)


def find_openssl() -> str | None:
    """openssl from the PATH, else the copy Git for Windows smuggles in."""
    if tool := shutil.which("openssl"):
        return tool
    for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("LOCALAPPDATA", "") + r"\Programs"):
        candidate = Path(root) / "Git" / "usr" / "bin" / "openssl.exe"
        if os.name == "nt" and candidate.exists():
            return str(candidate)
    return None


def make_certificate(host: str) -> tuple[bytes, bytes] | None:
    """(certificate + key for the Amiga, certificate for the PC); None when openssl is AWOL."""
    tool = find_openssl()
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
    path = FOLDER / f"tls-{safe(host)}.pem"
    path.write_bytes(certificate)
    return path
