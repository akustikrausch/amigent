"""Optional TLS to amigentd, the Amiga's cert pinned like a dongle: no CA, no DNS, no mercy.

amigent-setup cooks a small EC certificate (a 68k can chew that), puts
certificate and key into S:amigent-tls.pem on the Amiga and keeps the
certificate in ~/.config/amigent/tls-<host>.pem. With that file present the
clients speak TLS on the port after amigentd's; AMIGENT_TLS=off kills it.
The pin doubles as the Amiga's fingerprint when it turns up under a new
address (see machines.py).

AMIGENT_REQUIRE_TLS=1 turns "TLS when pinned" into "TLS or nothing": no pin,
AMIGENT_TLS=off, a refused handshake or a certificate that is not the pin all
end the attempt before a single byte goes to amigentd's plain port, and the
network is not swept for the Amiga either (the sweep knocks on plain ports).
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

from . import machines, roster, safety
from .paths import FOLDER, safe  # FOLDER stays patchable here, like secret.FOLDER


def pinned(host: str) -> Path:
    """This Amiga's pinned cert: by name, by any address it ever had, or by what DNS says."""
    names = roster.aliases(host)
    ident, blocked = roster.pin_conflicts(host)
    for name in names:
        if safe(name) in blocked:
            continue
        path = FOLDER / f"tls-{safe(name)}.pem"
        if path.exists():
            return path
    if blocked:
        raise roster.RegistryError(f"Ambiguous TLS pin for {host}. "
                                  f"Save its verified certificate as tls-{ident}.pem.")
    return FOLDER / f"tls-{safe(names[0])}.pem"


def wanted(host: str) -> bool:
    return os.environ.get("AMIGENT_TLS", "auto").lower() != "off" and pinned(host).exists()


AMISSL_OPEN, PLAIN_CLOSED = 2, 4  # hello flags: AmiSSL works; plain is for localhost only
PLAIN_CLOSED_TEXT = ("amigentd takes only encrypted connections from other computers. Copy its "
                     "certificate (tls-<id>.pem → tls-<address>.pem in ~/.config/amigent/ on the PC that set it up) "
                     "to this one, or run amigent-setup again.")
NO_TLS_TEXT = ("amigentd runs TLSONLY, but AmiSSL does not open there, so it turns every other "
               "computer away. Repair it at the Amiga: install AmiSSL again and run amigent-repair "
               "from the Amigent folder, or start amigentd SETUP there and run amigent-setup again.")

NO_CERT_TEXT = ("amigentd runs TLSONLY and AmiSSL works, but S:amigent-tls.pem does not load, so "
                "its encrypted port is closed and every other computer is turned away. Repair it "
                "at the Amiga: delete S:amigent-tls.pem and run amigent-repair from the Amigent "
                "folder, or start amigentd SETUP there and run amigent-setup again.")
HELLO_SECONDS = 1.0  # the plain hello after a refused TLS port; the Amiga is up, so it is quick


class TlsDown(ConnectionRefusedError):
    """The TLS port refused, and the plain port's hello says why."""


def plain_closed(flags: int, encrypted: bool) -> str:
    """Why a plain connection is about to be closed, from the hello flags; '' if it is not."""
    if encrypted or not flags & PLAIN_CLOSED:
        return ""
    return PLAIN_CLOSED_TEXT if flags & AMISSL_OPEN else NO_TLS_TEXT


REQUIRED_OFF_TEXT = ("AMIGENT_REQUIRE_TLS is set and so is AMIGENT_TLS=off; nothing was sent. "
                     "Drop one of the two.")
REQUIRED_NO_PIN_TEXT = ("AMIGENT_REQUIRE_TLS is set, but no certificate is pinned for {host} "
                        "(tls-<name>.pem in ~/.config/amigent/). Nothing was sent in the clear.")


class TlsRequired(ConnectionError):
    """AMIGENT_REQUIRE_TLS turned a plain connection down before it was made."""


def required_failure(host: str) -> str:
    """Why AMIGENT_REQUIRE_TLS cannot dial host over TLS, or '' when it can."""
    if os.environ.get("AMIGENT_TLS", "auto").lower() == "off":
        return REQUIRED_OFF_TEXT
    if not pinned(host).exists():
        return REQUIRED_NO_PIN_TEXT.format(host=host)
    return ""


def wrap(sock: socket.socket, host: str) -> ssl.SSLSocket:
    """TLS over sock; the pinned certificate gets in, nobody else."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False  # the pin is the identity; Amigas don't do DNS names
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(pinned(host)))
    return context.wrap_socket(sock)


def _why_tls_refused(address: str, port: int) -> str:
    """After the TLS port refused: what the plain port's hello says about it, or ''.

    Only the 8-byte hello is read; nothing at all is sent, no key and no command,
    and the line is closed at once.
    """
    try:
        with socket.create_connection((address, port), HELLO_SECONDS) as plain:
            plain.settimeout(HELLO_SECONDS)
            hello = b""
            while len(hello) < 8 and (chunk := plain.recv(8 - len(hello))):
                hello += chunk
    except OSError:
        return ""
    if len(hello) < 8 or hello[:4] != b"AMGT":
        return ""
    flags = int.from_bytes(hello[6:8], "big")
    if not flags & PLAIN_CLOSED:
        return ""
    return NO_CERT_TEXT if flags & AMISSL_OPEN else NO_TLS_TEXT


def _dial(address: str, host: str, port: int, timeout: float,
          handshake: float | None = None) -> socket.socket:
    if wanted(host):
        try:
            sock = socket.create_connection((address, port + 1), timeout)
        except ConnectionRefusedError as e:
            # Refused means the Amiga is up and the TLS port shut; an Amiga that is off
            # times out instead and costs no second try. The reason is read on the
            # plain port, so AMIGENT_REQUIRE_TLS goes without it.
            if not safety.require_tls() and (why := _why_tls_refused(address, port)):
                raise TlsDown(why) from e
            raise
        try:
            if handshake:
                sock.settimeout(handshake)
            return wrap(sock, host)
        except (OSError, ssl.SSLError):
            sock.close()
            raise
    if safety.require_tls():
        raise TlsRequired(required_failure(host) or REQUIRED_NO_PIN_TEXT.format(host=host))
    return socket.create_connection((address, port), timeout)


def connect(host: str, port: int, timeout: float) -> socket.socket:
    """Plain to port, or TLS to port + 1 when a certificate is pinned.

    host is a name or an address. No answer there? One sweep of the local
    network, and the Amiga is picked up again at its new address; not under
    AMIGENT_REQUIRE_TLS, where a failed TLS dial is the answer.
    """
    host = roster.identity(host)
    address = roster.address(host)  # machines is only rung when the line stays dead
    try:
        return _dial(address, host, port, timeout)
    except TlsDown:
        raise  # it answered where it was; nothing to look for
    except (OSError, ssl.SSLError):
        if not roster.is_id(host):
            raise  # a one-off address must never change the saved registry
        if safety.require_tls():
            raise
        moved = machines.relocate(host)
        if not moved or moved == address:
            raise
        return _dial(moved, host, port, timeout)


HANDSHAKE_WAIT = 30.0  # seconds; AmiSSL on a 68000 or over a plipbox takes its time


def greet(host: str, port: int, timeout: float, connect_timeout: float | None = None,
          handshake: float | None = None) -> bytes:
    """net.greet over TLS: amigentd's first bytes on port + 1, the pin checked; b"" for silence.

    The handshake gets connect_timeout too, unless handshake says otherwise."""
    try:
        with _dial(roster.address(host), host, port, connect_timeout or timeout, handshake) as s:
            s.settimeout(timeout)
            return s.recv(8)
    except OSError:  # ssl.SSLError and TlsRequired included
        return b""


def find_openssl() -> str | None:
    """AMIGENT_OPENSSL, else openssl from the PATH, else the copy Git for Windows smuggles in."""
    if chosen := os.environ.get("AMIGENT_OPENSSL"):
        return chosen
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
    path = FOLDER / f"tls-{safe(roster.identity(host))}.pem"
    path.write_bytes(certificate)
    return path


def provision(link, host: str, home: str) -> bool:
    """Hand amigentd (an open setup link) a certificate; keep the pin only if its TLS port
    answers with it within 30 s. No openssl, no crypto: plain it stays, and we say so."""
    made = make_certificate(host)
    if not made:
        print("No openssl on this PC, so the connection stays unencrypted.")
        return False
    for_amiga, certificate = made
    link.put("S:amigent-tls.pem", for_amiga)
    link.put(f"{home}/amigent-tls.pem", for_amiga)
    pin = remember(host, certificate)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            connect(host, link.port, 10).close()
            print("Connections to amigentd are encrypted from now on.")
            return True
        except OSError:
            time.sleep(3)
    pin.unlink()
    print("amigentd did not open its TLS port; the connection stays unencrypted.")
    return False


STARTUP = "RUN >NIL: C:amigentd"


def lock_plain(channel, home: str) -> bool:
    """TLS works, so bolt the plain door: amigentd starts with TLSONLY from the next boot.

    Only the bare startup line gets the switch, whatever else lives in
    S:User-Startup stays byte for byte. A marker in home tells amigent-repair to
    keep it that way after an OS update."""
    text = channel.read_file("S:User-Startup").decode("iso-8859-1")
    lines = text.split("\n")
    if not any(line.strip() == STARTUP for line in lines):
        return any(line.strip() == STARTUP + " TLSONLY" for line in lines)
    fixed = "\n".join(STARTUP + " TLSONLY" if line.strip() == STARTUP else line for line in lines)
    channel.write_file("S:User-Startup", fixed.encode("iso-8859-1"))
    channel.write_file(f"{home}/amigent.tlsonly", b"plain is closed, see S:User-Startup\n")
    return True
