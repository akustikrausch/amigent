"""Commands and files through amigentd (protocol 3), so amigactld is not needed.

AmigaChannel has the same methods as AmigaCtl. Commands() picks one of the two
when it connects: amigentd's command channel if the Amiga offers it, amigactld
otherwise. See docs/PROTOCOL.md.
"""

from __future__ import annotations

import socket
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import tls
from .amigactl import AmigaCtl
from .client import ExecResult, VolumeFlush, join_path
from .errors import AmigaError
from .secret import load_key

ENCODING = "iso-8859-1"
CMD_AUTH, CMD_CHANNEL = 17, 20
EXEC, READ, WRITE, DIR, STAT, DELETE, RENAME, MAKEDIR, CHECKSUM, SYSINFO, VOLUMES = range(21, 32)
CHANNEL_PROTOCOL = 3
PAUSE = 19  # also on the screen connection: x = seconds, 0 ends it
PAUSE_PROTOCOL = 4
PAUSE_MAX = 65535  # seconds; INFO tells what is left in a u16
PLAIN_CLOSED = 4  # hello flag: amigentd runs TLSONLY, plain is for localhost only
PLAIN_CLOSED_TEXT = ("amigentd takes only encrypted connections from other computers. Copy its "
                     "certificate (tls-<name>.pem in ~/.config/amigent/ on the PC that set it up) "
                     "to this one, or run amigent-setup again.")
SLICE = 16384  # send granularity: a stall is noticed per slice, not per file
WINDOW = 16  # requests in flight; enough to hide a 300 ms round trip behind 4 KB files
CRC_RATE = 50_000  # bytes/s CHECKSUM manages on a 7 MHz 68000 reading a slow disk
DISK_RATE = 20_000  # bytes/s for the far end to settle a READ/WRITE, pessimistic on purpose


def budget(size: int, rate: float, base: float = 30.0) -> float:
    """Seconds to wait for an answer that costs the Amiga size bytes of work at rate."""
    return base + size / rate


def parse_entry(line: str) -> dict:
    kind, name, size, protection, stamp = line.split("\t")
    return {"type": kind, "name": name, "size": int(size), "protection": protection, "date": stamp}


def _read_text(ch) -> list[str]:
    size = struct.unpack(">I", ch._exact(4))[0]
    return ch._exact(size).decode(ENCODING).splitlines()


def _read_blob(ch) -> bytes:
    return ch._exact(struct.unpack(">I", ch._exact(4))[0])


def _read_crc(ch) -> int:
    return struct.unpack(">I", ch._exact(4))[0]


def _nothing(ch) -> None:
    return None


@dataclass
class Request:
    """One channel request: header fields, payload, how to read a success, and patience."""
    cmd: int
    path: str = ""
    arg: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    payload: bytes = b""
    read: Callable = field(default=_nothing)
    wait: float = 0.0  # seconds the Amiga may chew on it before it answers

    @classmethod
    def write(cls, path: str, data: bytes, append: bool = False) -> "Request":
        return cls(WRITE, path, int(append), 0, len(data) >> 16, len(data) & 0xFFFF, data,
                   wait=budget(len(data), DISK_RATE))

    @classmethod
    def read_file(cls, path: str, size: int = 0) -> "Request":
        return cls(READ, path, read=_read_blob, wait=budget(size, DISK_RATE))

    @classmethod
    def checksum(cls, path: str, size: int = 0) -> "Request":
        return cls(CHECKSUM, path, read=_read_crc, wait=budget(size, CRC_RATE))

    @classmethod
    def listing(cls, path: str) -> "Request":
        return cls(DIR, path, read=lambda ch: [parse_entry(x) for x in _read_text(ch)])

    @classmethod
    def stat(cls, path: str) -> "Request":
        return cls(STAT, path, read=lambda ch: parse_entry(_read_text(ch)[0]))

    @classmethod
    def makedir(cls, path: str) -> "Request":
        return cls(MAKEDIR, path)

    @classmethod
    def exec(cls, command: str, cd: str | None = None, timeout: float | None = None) -> "Request":
        where = (cd or "").encode(ENCODING)
        seconds = min(int(timeout), 0xFFFF) if timeout else 0
        return cls(EXEC, command, 0, len(where), seconds, 0, where, read=_read_exec,
                   wait=(timeout + 10) if timeout else 0.0)


def _read_exec(ch) -> ExecResult:
    rc = struct.unpack(">i", ch._exact(4))[0]
    return ExecResult(rc, _read_blob(ch).decode(ENCODING))


def perform(ctl, requests: list[Request]) -> list:
    """Requests as one pipelined batch where the backend can, one by one where it can't.

    Answers line up with requests: a result or the AmigaError the Amiga gave.
    amigactld has no pipeline, so it walks through them with the plain methods."""
    backend = getattr(ctl, "backend", ctl)
    if isinstance(backend, AmigaChannel):
        return backend.pipeline(requests)
    calls = {
        EXEC: lambda r: ctl.exec(r.path, r.payload.decode(ENCODING) or None, r.w or None),
        WRITE: lambda r: ctl.write_file(r.path, r.payload, append=bool(r.arg)),
        READ: lambda r: ctl.read_file(r.path),
        STAT: lambda r: ctl.stat(r.path),
        DIR: lambda r: ctl.list_dir(r.path),
        MAKEDIR: lambda r: ctl.makedir(r.path),
        CHECKSUM: lambda r: ctl.checksum(r.path),
    }
    out = []
    for r in requests:
        try:
            out.append(calls[r.cmd](r))
        except AmigaError as e:
            if e.code is None:
                raise
            out.append(e)
    return out


class AmigaChannel(VolumeFlush):
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
            self._sock = tls.connect(self.host, self.port, self.timeout)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            hello = self._exact(8)
            if hello[:4] != b"AMGT":
                raise AmigaError(f"unexpected greeting {hello!r}")
            self.protocol, flags = struct.unpack(">HH", hello[4:8])
            if self.protocol < CHANNEL_PROTOCOL:
                raise AmigaError(f"amigentd speaks protocol {self.protocol}, commands need "
                                 f"{CHANNEL_PROTOCOL}")
            if flags & PLAIN_CLOSED and not hasattr(self._sock, "version"):
                raise AmigaError(PLAIN_CLOSED_TEXT)
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

    def _send(self, data: bytes) -> None:
        """sendall in slices: its timeout covers the whole call, so 5 MB at 15 KB/s
        would trip any fixed value. Per slice it means 'the wire stalled', which is
        the only thing worth timing out on."""
        assert self._sock
        view = memoryview(data)
        for at in range(0, len(view), SLICE):
            self._sock.sendall(view[at:at + SLICE])

    def _put(self, req: Request) -> None:
        """Fire one request at the Amiga without waiting for its answer."""
        raw = req.path.encode(ENCODING)
        head = struct.pack(">BBHHHH", req.cmd, req.arg, len(raw), req.y, req.w, req.h)
        self._send(head + raw + req.payload)  # one segment, not three: plip pays per packet

    def _status(self) -> None:
        """First byte of every answer; a refusal carries its text and nothing else."""
        status = self._exact(1)[0]
        if status:
            size = struct.unpack(">H", self._exact(2))[0]
            raise AmigaError(self._exact(size).decode(ENCODING) or f"error {status}", status)

    def _request(self, cmd: int, path: str = "", arg: int = 0, y: int = 0, w: int = 0, h: int = 0,
                 payload: bytes = b"", timeout: float | None = None) -> None:
        """Send a request; raise AmigaError with code for a refusal."""
        if not self._sock:
            self.connect()
        self._sock.settimeout(timeout if timeout is not None else self.timeout)
        self._put(Request(cmd, path, arg, y, w, h, payload))
        self._status()

    def _text(self, cmd: int, path: str = "") -> list[str]:
        self._request(cmd, path)
        return _read_text(self)

    def pipeline(self, requests: list[Request], window: int = WINDOW) -> list:
        """Many requests, one wire: keep up to window of them in flight.

        The channel works through requests strictly in order and WRITE swallows
        its data even when it fails, so the stream never loses sync. Each answer
        lands in the list at its request's place: the reader's result, or the
        AmigaError the Amiga sent. Only a broken link raises (and closes).
        Requests are small and answers to WRITE are one byte, so neither side
        can wedge the other with a full buffer.
        """
        if not self._sock:
            self.connect()
        results: list = [None] * len(requests)
        sent = got = 0
        try:
            while got < len(requests):
                while sent < len(requests) and sent - got < window:
                    self._sock.settimeout(self.timeout)
                    self._put(requests[sent])
                    sent += 1
                self._sock.settimeout(max(requests[got].wait, self.timeout))
                try:
                    self._status()
                    results[got] = requests[got].read(self)
                except AmigaError as e:
                    if e.code is None:
                        raise
                    results[got] = e
                got += 1
        except (OSError, AmigaError):
            self.close()  # half-read answers: nothing after this point lines up any more
            raise
        return results

    # files

    def list_dir(self, path: str, recursive: bool = False) -> list[dict]:
        if not recursive:
            return [parse_entry(line) for line in self._text(DIR, path)]
        return [e for e in self.walk(path) if not isinstance(e, AmigaError)]

    def walk(self, path: str) -> list:
        """Recursive DIR: one pipelined batch per depth level, not one round trip per folder.

        Names come relative to path ('sub/file'), parents before children. A folder
        that can't be listed shows up as its AmigaError, with .folder set.
        """
        out: list = []
        level = [""]
        while level:
            answers = self.pipeline([Request.listing(join_path(path, rel) if rel else path)
                                     for rel in level])
            deeper = []
            for rel, answer in zip(level, answers):
                if isinstance(answer, AmigaError):
                    if not rel:
                        raise answer  # the top itself: that one is the caller's problem
                    answer.folder = rel
                    out.append(answer)
                    continue
                for entry in answer:
                    name = f"{rel}/{entry['name']}" if rel else entry["name"]
                    out.append({**entry, "name": name})
                    if entry["type"] == "DIR":
                        deeper.append(name)
            level = deeper
        return out

    def stat(self, path: str) -> dict:
        kind, name, size, protection, stamp = self._text(STAT, path)[0].split("\t")
        return {"type": kind.lower(), "name": name, "size": size, "protection": protection,
                "datestamp": stamp}

    def read_file(self, path: str) -> bytes:
        return self._one(Request.read_file(path))

    def write_file(self, path: str, data: bytes, append: bool = False) -> None:
        self._one(Request.write(path, data, append))

    def checksum(self, path: str, size: int = 0) -> int:
        """CRC-32 of the file; its size, if known, buys the Amiga time to read it."""
        return self._one(Request.checksum(path, size))

    def _one(self, req: Request):
        """A single request with its own patience. No answer means the link is gone."""
        if not self._sock:
            self.connect()
        try:
            self._sock.settimeout(self.timeout)
            self._put(req)
            self._sock.settimeout(max(req.wait, self.timeout))
            self._status()
            return req.read(self)
        except OSError:
            self.close()  # a late answer must not be taken for the next request's
            raise

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
        return self._one(Request.exec(command, cd, timeout))

    # system state

    def sysinfo(self) -> dict:
        return dict(line.split("=", 1) for line in self._text(SYSINFO) if "=" in line)

    def pause_screen(self, seconds: int) -> int:
        """Stop amigentd reading the screen for seconds, 0 to go on; the seconds left after.

        While paused every frame comes back empty and nothing reads the screen,
        whoever asks: the viewer, screenshots, wait_change. Mouse, keys, commands
        and files carry on. A new pause replaces the running one.
        """
        if not self._sock:
            self.connect()
        if self.protocol < PAUSE_PROTOCOL:
            raise AmigaError(f"amigentd on the Amiga speaks protocol {self.protocol}, pausing the "
                             f"screen needs {PAUSE_PROTOCOL}: update amigentd (amigent-setup)")
        self._sock.settimeout(self.timeout)
        self._header(PAUSE, 0, max(0, min(int(seconds), PAUSE_MAX)))
        self._status()
        return self.screen_paused()

    def screen_paused(self) -> int:
        """Seconds the screen pause still runs, 0 when there is none."""
        return int(self.sysinfo().get("screen_paused", 0))

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
    def _sock(self):  # viewer.py still peeks here; new code asks connected / is_tls
        return self.backend._sock if self.backend else None

    @property
    def connected(self) -> bool:
        return bool(self._sock)

    @property
    def is_tls(self) -> bool:
        return hasattr(self._sock, "version")  # only an SSLSocket knows its protocol version

    @property
    def banner(self) -> str:
        return self.backend.banner if self.backend else ""

    def __getattr__(self, name):
        if self.backend is None:
            self.connect()
        return getattr(self.backend, name)
