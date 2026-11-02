"""Commands and files through amigentd (protocol 3), so amigactld is not needed.

AmigaChannel has the same methods as AmigaCtl. Commands() picks one of the two
when it connects: amigentd's command channel if the Amiga offers it, amigactld
otherwise. See docs/PROTOCOL.md.
"""

from __future__ import annotations

import socket
import struct

from .amigactl import AmigaCtl, AmigaError, ExecResult
from .secret import load_key

ENCODING = "iso-8859-1"
CMD_AUTH, CMD_CHANNEL = 17, 20
EXEC, READ, WRITE, DIR, STAT, DELETE, RENAME, MAKEDIR, CHECKSUM, SYSINFO, VOLUMES = range(21, 32)
CHANNEL_PROTOCOL = 3


class AmigaChannel:
    """A command channel to amigentd. Errors come back as AmigaError with a code."""

    def __init__(self, host: str, port: int = 6801, timeout: float = 30.0, key: str | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.key = key
        self._sock: socket.socket | None = None
        self.banner = ""
        self.protocol = 0

    # link

    def connect(self) -> None:
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = self._exact(8)
            if hello[:4] != b"AMGT":
                raise AmigaError(f"unexpected greeting {hello!r}")
            self.protocol, flags = struct.unpack(">HH", hello[4:8])
            if self.protocol < CHANNEL_PROTOCOL:
                raise AmigaError(f"amigentd speaks protocol {self.protocol}, commands need "
                                 f"{CHANNEL_PROTOCOL}")
            if flags & 1:
                key = self.key if self.key is not None else load_key(self.host)
                if not key:
                    raise AmigaError("amigentd wants a key: run amigent-setup, or set AMIGENT_KEY")
                raw = key.encode()
                self._header(CMD_AUTH, 0, len(raw))
                self._sock.sendall(raw)
                if self._exact(1) != b"\0":
                    raise AmigaError("amigentd refused the key")
            self._header(CMD_CHANNEL)
            if self._exact(1) != b"\0":
                raise AmigaError("amigentd refused a command channel")
            self.banner = f"amigentd protocol {self.protocol}"
        except OSError as e:
            self.close()
            raise AmigaError(f"cannot reach amigentd at {self.host}:{self.port}: {e}") from e
        except AmigaError:
            self.close()
            raise

    def close(self) -> None:
        if self._sock:
            self._sock.close()
        self._sock = None

    def _exact(self, n: int) -> bytes:
        assert self._sock
        data = bytearray()
        while len(data) < n:
            chunk = self._sock.recv(min(n - len(data), 65536))
            if not chunk:
                raise AmigaError("connection closed by amigentd")
            data += chunk
        return bytes(data)

    def _header(self, cmd: int, arg: int = 0, x: int = 0, y: int = 0, w: int = 0, h: int = 0) -> None:
        assert self._sock
        self._sock.sendall(struct.pack(">BBHHHH", cmd, arg, x, y, w, h))

    def _request(self, cmd: int, path: str = "", arg: int = 0, y: int = 0, w: int = 0, h: int = 0,
                 payload: bytes = b"", timeout: float | None = None) -> None:
        """Send a request; raise AmigaError with code for a refusal."""
        if not self._sock:
            self.connect()
        raw = path.encode(ENCODING)
        self._sock.settimeout(timeout if timeout is not None else self.timeout)
        self._header(cmd, arg, len(raw), y, w, h)
        if raw or payload:
            self._sock.sendall(raw + payload)
        status = self._exact(1)[0]
        if status:
            size = struct.unpack(">H", self._exact(2))[0]
            raise AmigaError(self._exact(size).decode(ENCODING) or f"error {status}", status)

    def _text(self, cmd: int, path: str = "") -> list[str]:
        self._request(cmd, path)
        size = struct.unpack(">I", self._exact(4))[0]
        return self._exact(size).decode(ENCODING).splitlines()

    # files

    def list_dir(self, path: str, recursive: bool = False) -> list[dict]:
        entries = []
        for line in self._text(DIR, path):
            kind, name, size, protection, stamp = line.split("\t")
            entries.append({"type": kind, "name": name, "size": int(size),
                            "protection": protection, "date": stamp})
        if recursive:
            for entry in [e for e in entries if e["type"] == "DIR"]:
                folder = path + entry["name"] if path.endswith((":", "/")) else f"{path}/{entry['name']}"
                for sub in self.list_dir(folder, True):
                    entries.append({**sub, "name": f"{entry['name']}/{sub['name']}"})
        return entries

    def stat(self, path: str) -> dict:
        kind, name, size, protection, stamp = self._text(STAT, path)[0].split("\t")
        return {"type": kind.lower(), "name": name, "size": size, "protection": protection,
                "datestamp": stamp}

    def read_file(self, path: str) -> bytes:
        self._request(READ, path, timeout=max(self.timeout, 300))
        return self._exact(struct.unpack(">I", self._exact(4))[0])

    def write_file(self, path: str, data: bytes, append: bool = False) -> None:
        self._request(WRITE, path, arg=int(append), w=len(data) >> 16, h=len(data) & 0xFFFF,
                      payload=data, timeout=max(self.timeout, 300))

    def checksum(self, path: str) -> int:
        self._request(CHECKSUM, path)
        return struct.unpack(">I", self._exact(4))[0]

    def delete(self, path: str) -> None:
        self._request(DELETE, path)

    def rename(self, old: str, new: str) -> None:
        raw = new.encode(ENCODING)
        self._request(RENAME, old, y=len(raw), payload=raw)

    def makedir(self, path: str) -> None:
        self._request(MAKEDIR, path)

    # commands

    def exec(self, command: str, cd: str | None = None, timeout: float | None = None) -> ExecResult:
        """Run a command line; other channels and the screen stay usable meanwhile."""
        where = (cd or "").encode(ENCODING)
        seconds = int(timeout) if timeout else 0
        self._request(EXEC, command, y=len(where), w=min(seconds, 0xFFFF), payload=where,
                      timeout=(timeout + 10) if timeout else None)
        rc = struct.unpack(">i", self._exact(4))[0]
        output = self._exact(struct.unpack(">I", self._exact(4))[0])
        return ExecResult(rc, output.decode(ENCODING))

    def flush(self, volumes) -> None:
        for volume in volumes:
            self.exec(f"Lock {volume} ON")
            self.exec(f"Lock {volume} OFF")

    # system state

    def sysinfo(self) -> dict:
        return dict(line.split("=", 1) for line in self._text(SYSINFO) if "=" in line)

    def volumes(self) -> list[dict]:
        keys = ("name", "used", "free", "capacity", "blocksize")
        return [{k: (v if k == "name" else int(v)) for k, v in zip(keys, line.split("\t"))}
                for line in self._text(VOLUMES)]


class Commands:
    """amigentd's command channel when the Amiga offers it, else amigactld."""

    def __init__(self, host: str, ctl_port: int = 6800, screen_port: int = 6801,
                 timeout: float = 30.0):
        self.host, self.ctl_port, self.screen_port, self.timeout = host, ctl_port, screen_port, timeout
        self.backend: AmigaChannel | AmigaCtl | None = None

    def connect(self) -> None:
        if self.backend is None:
            channel = AmigaChannel(self.host, self.screen_port, self.timeout)
            try:
                channel.connect()
                self.backend = channel
                return
            except AmigaError as e:
                if "refused the key" in str(e) or "wants a key" in str(e):
                    raise
            self.backend = AmigaCtl(self.host, self.ctl_port, self.timeout)
        self.backend.connect()

    def close(self) -> None:
        if self.backend:
            self.backend.close()

    @property
    def _sock(self):
        return self.backend._sock if self.backend else None

    @property
    def banner(self) -> str:
        return self.backend.banner if self.backend else ""

    def __getattr__(self, name):
        if self.backend is None:
            self.connect()
        return getattr(self.backend, name)
