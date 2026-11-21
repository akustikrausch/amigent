"""Commands and files through amigentd (protocol 3), so amigactld is not needed.

AmigaChannel has the same methods as AmigaCtl. Commands() picks one of the two
when it connects: amigentd's command channel if the Amiga offers it, amigactld
otherwise. See docs/PROTOCOL.md.
"""

from __future__ import annotations

import ipaddress
import socket
import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Callable

from . import pack, tls
from .amigactl import AmigaCtl
from .client import ExecResult, VolumeFlush, join_path
from .errors import AmigaError
from .secret import load_key

ENCODING = "iso-8859-1"
CMD_AUTH, CMD_CHANNEL = 17, 20
EXEC, READ, WRITE, DIR, STAT, DELETE, RENAME, MAKEDIR, CHECKSUM, SYSINFO, VOLUMES = range(21, 32)
TEXT, WAIT, COMPRESS, TREE, DEBUGLOG = range(32, 37)
NOTE = 37  # a line for amigent-monitor, no answer; SYSINFO says "note=1" when understood
LOADED, PEEK, TASKS, TASKCTL, POKE, TRACE = 38, 39, 40, 41, 42, 43  # protocol 6
LOCKS, CPU, DRAG, QUIET = 44, 45, 46, 47  # protocol 6, "probe" in SYSINFO
AUDIO = 48  # Apollo build only, and only when SYSINFO says audio=1
# Opcode 49 is reserved for PIP. Nothing in this tree sends it.
PEEK_MAX = 65536
NOTE_MAX = 160
CHANNEL_PROTOCOL = 3
PACK_PROTOCOL = 5  # TEXT, WAIT, COMPRESS, TREE, DEBUGLOG, packed replies and writes
WAIT_WINDOW, WAIT_WINDOW_GONE, WAIT_TEXT, WAIT_TEXT_GONE, WAIT_NEW, WAIT_KNOWN = 1, 2, 3, 4, 8, 16
PACK_WRITE_MIN = 512  # bytes; below this packing a WRITE is not worth the bother
PAUSE = 19  # also on the screen connection: x = seconds, 0 ends it
PAUSE_PROTOCOL = 4
LOADED_PROTOCOL = 6
NEWEST_PROTOCOL = LOADED_PROTOCOL  # the newest amigentd this Amigent knows


def pc_outdated(protocol: int) -> str:
    """A note when the Amiga runs a newer amigentd than this PC knows, else ''."""
    if protocol <= NEWEST_PROTOCOL:
        return ""
    from . import __version__
    return (f"amigentd on the Amiga is newer than Amigent {__version__} on this PC. Update the PC "
            "side too: run \"pipx upgrade amigent\", then restart the AI app and the remote desktop.")


def feature_opcode(protocol: int, feature: str, info: dict | None = None) -> int | None:
    """The opcode for one feature on this daemon, or None when it is not offered.

    Protocol 6 uses the upstream map. Opcode 38 is LOADED there, and AUDIO is 48
    only when SYSINFO says ``audio=1``. Opcode 49 is reserved for PIP and is
    returned only when that daemon advertises ``pip=1``. Protocol 5 uses the
    published AmanitaHouse map, and only when SYSINFO advertises it: AUDIO 38,
    PIP 39, LOADED 40 when ``modules=1``, TASKS 41 when ``tasklist=1``.
    """
    info = info or {}
    if protocol >= LOADED_PROTOCOL:
        if feature == "AUDIO":
            return AUDIO if info.get("audio") == "1" else None
        if feature == "PIP":
            return 49 if info.get("pip") == "1" else None
        if feature == "LOADED" and info.get("loaded") == "1":
            return LOADED
        if feature == "TASKS" and info.get("loaded") == "1":
            return TASKS
        if feature == "PEEK" and info.get("loaded") == "1":
            return PEEK
        if feature == "TASKCTL" and info.get("loaded") == "1":
            return TASKCTL
        if feature == "TRACE" and info.get("loaded") == "1":
            return None if info.get("trace") == "0" else TRACE
        if feature == "POKE":
            return POKE
        return None
    if protocol != PACK_PROTOCOL:
        return None
    legacy = {"AUDIO": (38, "audio"), "PIP": (39, "pip"),
              "LOADED": (40, "modules"), "TASKS": (41, "tasklist")}
    if feature not in legacy:
        return None
    opcode, flag = legacy[feature]
    return opcode if info.get(flag) == "1" else None


def audio_request(arg: int = 0, y: int = 0, protocol: int = LOADED_PROTOCOL) -> bytes:
    """The 10-byte AUDIO header. arg 1 starts a stream, arg 2 stops, y is a one-shot in ms.

    Protocol 6 is opcode 48. Protocol 5 is opcode 38, and only a daemon that
    advertised ``audio=1`` may be sent it.
    """
    opcode = feature_opcode(protocol, "AUDIO", {"audio": "1"})
    if opcode is None:
        raise AmigaError("AUDIO is not available on this protocol")
    return struct.pack(">BBHHHH", opcode, arg & 0xFF, 0, y & 0xFFFF, 0, 0)


PAUSE_MAX = 65535  # seconds; INFO tells what is left in a u16
PLAIN_CLOSED = 4  # hello flag: amigentd runs TLSONLY, plain is for localhost only
PLAIN_CLOSED_TEXT = ("amigentd takes only encrypted connections from other computers. Copy its "
                     "certificate (tls-<name>.pem in ~/.config/amigent/ on the PC that set it up) "
                     "to this one, or run amigent-setup again.")
SLICE = 16384  # send granularity: a stall is noticed per slice, not per file
WINDOW = 16  # requests in flight; enough to hide a 300 ms round trip behind 4 KB files
CRC_RATE = 50_000  # bytes/s CHECKSUM manages on a 7 MHz 68000 reading a slow disk
DISK_RATE = 20_000  # bytes/s for the far end to settle a READ/WRITE, pessimistic on purpose


def peer_is_loopback(host: str) -> bool:
    """127.0.0.1 and localhost. The TLS pump is one of these. A name is not."""
    text = str(host).split("%", 1)[0]
    if text.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def audio_uses_tls(flags: int, host: str) -> bool:
    """Remote audio goes over TLS when the daemon has closed the plain port."""
    return bool(flags & PLAIN_CLOSED) and not peer_is_loopback(host)


def apollo_commands(info: dict | None) -> frozenset[str]:
    """AUDIO (and PIP only when SYSINFO says ``pip=1``) offered by this daemon.

    This contribution streams AUDIO only. ``pip=0`` is not a picture.
    """
    info = info or {}
    commands = set()
    if info.get("audio") == "1":
        commands.add("AUDIO")
    if info.get("pip") == "1":
        commands.add("PIP")
    return frozenset(commands)


def pcm_wav(pcm: bytes, rate: int) -> bytes:
    """s16le stereo at rate, as a WAV the host tools can play."""
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 2, rate, rate * 4, 4, 16,
        b"data", len(pcm))
    return header + pcm


def budget(size: int, rate: float, base: float = 30.0) -> float:
    """Seconds to wait for an answer that costs the Amiga size bytes of work at rate."""
    return base + size / rate


def parse_entry(line: str) -> dict:
    kind, name, size, protection, stamp = line.split("\t")
    return {"type": kind, "name": name, "size": int(size), "protection": protection, "date": stamp}


def _read_text(ch) -> list[str]:
    return _read_blob(ch).decode(ENCODING).splitlines()


def _read_blob(ch) -> bytes:
    """u32 length, then the bytes: as they are, or packed frames after COMPRESS."""
    size = struct.unpack(">I", ch._exact(4))[0]
    return ch._body(size)


def unpack_frames(read, size: int) -> tuple[bytes, int]:
    """Frames (u32 length, data) of one raw deflate stream, ended by a 0: the bytes
    and how many came over the wire. read(n) returns exactly n bytes."""
    inflater = zlib.decompressobj(-15)
    out, wire = [], 0
    while True:
        n = struct.unpack(">I", read(4))[0]
        wire += 4
        if not n:
            break
        out.append(inflater.decompress(read(n)))
        wire += n
    out.append(inflater.flush())
    data = b"".join(out)
    if len(data) != size:
        raise AmigaError(f"packed reply came to {len(data)} bytes instead of {size}")
    return data, wire


def _read_debug(ch) -> tuple[int, bytes]:
    total, size = struct.unpack(">II", ch._exact(8))
    return total, ch._exact(size)


def _read_crc(ch) -> int:
    return struct.unpack(">I", ch._exact(4))[0]


def _nothing(ch) -> None:
    return None


DOS_HINTS = {
    202: "another program has it open for writing; AmigaDOS lets nobody read it "
         "until that program closes it",
    218: "that volume or assign isn't there; assigns made by hand or by a start "
         "script are gone after a restart; Assign NAME: folder makes one again",
}


def dos_text(text: str) -> str:
    """'Objekt ist in Gebrauch [202]' from amigentd 1.3.0 -> the text with a hint."""
    m = re.fullmatch(r"(.*?)\s*\[(\d+)\]", text, re.S)
    if not m:
        return text
    hint = DOS_HINTS.get(int(m.group(2)))
    return f"{m.group(1)} ({hint})" if hint else m.group(1)


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
    def tree(cls, path: str, crc: bool = False, most: int = 0, wait: float = 600.0) -> "Request":
        return cls(TREE, path, int(crc), 0, min(most, 0xFFFF), 0,
                   read=lambda ch: parse_tree(_read_text(ch)), wait=wait)

    @classmethod
    def exec(cls, command: str, cd: str | None = None, timeout: float | None = None) -> "Request":
        where = (cd or "").encode(ENCODING)
        seconds = min(int(timeout), 0xFFFF) if timeout else 0
        return cls(EXEC, command, 0, len(where), seconds, 0, where, read=_read_exec,
                   wait=(timeout + 10) if timeout else 0.0)


def parse_tree(lines: list[str]) -> list[dict]:
    """TREE lines: entries as DIR has them with the path as name, crc when asked;
    'ERR path code' for folders that could not be read, 'MORE' when cut short."""
    out = []
    for line in lines:
        parts = line.split("\t")
        if parts[0] in ("FILE", "DIR") and len(parts) >= 5:
            entry = {"type": parts[0], "name": parts[1], "size": int(parts[2]),
                     "protection": parts[3], "date": parts[4]}
            if len(parts) > 5:
                entry["crc"] = None if parts[5] == "-" else int(parts[5], 16)
            out.append(entry)
        elif parts[0] == "ERR":
            out.append({"type": "ERR", "name": parts[1] if len(parts) > 1 else "",
                        "error": parts[2] if len(parts) > 2 else ""})
        elif parts[0] == "MORE":
            out.append({"type": "MORE"})
    return out


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
        self.policy: pack.Policy | None = None  # Commands hands its own; else one per host
        self.zlevel = 0  # what COMPRESS last set on this connection
        self._asked = 0.0  # when the request whose answer comes next went out
        self._audio_opcode = 0

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
                if flags & 2:
                    raise AmigaError(PLAIN_CLOSED_TEXT)
                from .tls import TLS_UNAVAILABLE_TEXT
                raise AmigaError(TLS_UNAVAILABLE_TEXT)
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
            self.zlevel = 0
            self._notes = self._read_from = None  # a new amigentd may answer now
            self._audio_opcode = 0
            if self.protocol >= PACK_PROTOCOL:
                if self.policy is None:
                    self.policy = pack.policy_for(self.host)
                if not self.policy.known:
                    self.policy.learn(self.sysinfo())
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
        arg, w, h, payload = req.arg, req.w, req.h, req.payload
        if (req.cmd == WRITE and len(payload) >= PACK_WRITE_MIN and self.policy
                and self.protocol >= PACK_PROTOCOL and self.policy.upload()):
            packer = zlib.compressobj(6, zlib.DEFLATED, -15)
            packed = packer.compress(payload) + packer.flush()
            if len(packed) < len(payload) * 0.9:
                arg, w, h, payload = arg | 2, len(packed) >> 16, len(packed) & 0xFFFF, packed
        head = struct.pack(">BBHHHH", req.cmd, arg, len(raw), req.y, w, h)
        self._asked = time.monotonic()  # before any byte of its answer can come
        self._send(head + raw + payload)  # one segment, not three: plip pays per packet

    def _body(self, size: int) -> bytes:
        """size bytes of a reply body, unpacked if packing is on; the line is measured."""
        start = time.monotonic()
        if self.zlevel:
            data, wire = unpack_frames(self._exact, size)
        else:
            data, wire = self._exact(size), size
        if self.policy and self.zlevel:  # it waited on the Amiga's packing too: a floor
            self.policy.note(wire, time.monotonic() - self._asked, at_least=True)
        elif self.policy:
            self.policy.note(wire, time.monotonic() - start)
        return data

    def _sync_packing(self) -> None:
        """Tell amigentd the packing level the policy wants, when it changed."""
        if self.protocol < PACK_PROTOCOL or not self.policy:
            return
        level = self.policy.level()
        if level == self.zlevel:
            return
        self._sock.settimeout(self.timeout)
        self._header(COMPRESS, level)
        try:
            self._status()
            self.zlevel = level
        except AmigaError as e:
            if e.code is None:
                raise
            self.policy.mode = "off"  # no memory for it on the Amiga: leave it

    def _status(self) -> None:
        """First byte of every answer; a refusal carries its text and nothing else."""
        status = self._exact(1)[0]
        if status:
            size = struct.unpack(">H", self._exact(2))[0]
            raise AmigaError(dos_text(self._exact(size).decode(ENCODING)) or f"error {status}",
                             status)

    def _request(self, cmd: int, path: str = "", arg: int = 0, y: int = 0, w: int = 0, h: int = 0,
                 payload: bytes = b"", timeout: float | None = None) -> None:
        """Send a request; raise AmigaError with code for a refusal."""
        if not self._sock:
            self.connect()
        self._sync_packing()
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
        asked = [0.0] * len(requests)
        sent = got = 0
        try:
            self._sync_packing()
            while got < len(requests):
                while sent < len(requests) and sent - got < window:
                    self._sock.settimeout(self.timeout)
                    self._put(requests[sent])
                    asked[sent] = self._asked
                    sent += 1
                self._sock.settimeout(max(requests[got].wait, self.timeout))
                self._asked = asked[got]  # its answer is the one that comes now
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

    def read_from(self, path: str, start: int) -> bytes:
        """The file from byte start on, for following a growing log. amigentd before
        1.3.0 sends all of it; the cut happens here then."""
        if getattr(self, "_read_from", None) is None:
            self._read_from = self.sysinfo().get("readfrom") == "1"
        if not self._read_from or start <= 0:
            return self.read_file(path)[max(start, 0):]
        return self._one(Request(READ, path, 1, 0, (start >> 16) & 0xFFFF, start & 0xFFFF,
                                 read=_read_blob, wait=budget(0, DISK_RATE)))

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
            self._sync_packing()
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
                             f"screen needs {PAUSE_PROTOCOL}: run amigent-setup --update")
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

    # protocol 5

    def _need5(self, what: str) -> None:
        if not self._sock:
            self.connect()
        if self.protocol < PACK_PROTOCOL:
            raise AmigaError(f"{what} needs amigentd 1.3.0 or newer (protocol {PACK_PROTOCOL}); "
                             "run amigent-setup --update")

    def screen_text(self) -> list[str]:
        """The front screen's text as amigentd recorded it: I, W and L lines."""
        self._need5("reading the screen's text")
        return self._text(TEXT)

    def wait_for(self, kind: int, pattern: str, timeout: float,
                 known: list[int] | None = None) -> list[str]:
        """Until a window or text turns up (or goes); the line that matched. A
        timeout is an AmigaError with code 2. known: window addresses that do
        not count as new (with WAIT_NEW), as seen before a program started."""
        self._need5("waiting on the Amiga")
        seconds = max(1, min(int(timeout + 0.999), 3600))
        ids = b""
        if known is not None and kind & WAIT_NEW:
            known = known[:32]
            kind |= WAIT_KNOWN
            ids = b"".join(struct.pack(">I", k & 0xFFFFFFFF) for k in known)
        return self._one(Request(WAIT, pattern, kind, len(ids) // 4, seconds, 0, ids,
                                 read=_read_text, wait=seconds + 15))

    def tree(self, path: str, crc: bool = False, most: int = 0) -> list[dict]:
        self._need5("listing a whole tree at once")
        return self._one(Request.tree(path, crc, most))

    def note(self, text: str) -> None:
        """A line for amigent-monitor's window on the Amiga: what the agent is doing.

        NOTE has no answer, so it costs no round trip and the stream stays in
        step. Silently nothing on an amigentd that doesn't know it."""
        if not self._sock or self.protocol < PACK_PROTOCOL:
            return
        if getattr(self, "_notes", None) is None:
            self._notes = self.sysinfo().get("note") == "1"
        if not self._notes:
            return
        raw = text.encode(ENCODING, "replace")[:NOTE_MAX]
        self._sock.settimeout(self.timeout)
        self._send(struct.pack(">BBHHHH", NOTE, 0, len(raw), 0, 0, 0) + raw)

    # protocol 6

    def _need6(self, what: str) -> None:
        if not self._sock:
            self.connect()
        if self.protocol < LOADED_PROTOCOL:
            raise AmigaError(f"{what} needs amigentd 1.4.0 or newer (protocol {LOADED_PROTOCOL}); "
                             "run amigent-setup --update")

    def loaded(self) -> list[str]:
        """LOADED lines: L/D/R libraries, devices, resources; H hunks; M resident modules."""
        self._need6("listing what is loaded")
        return self._text(LOADED)

    def peek(self, address: int, size: int) -> bytes:
        """Memory as it is: only RAM exec manages and the Kickstart ROM, at most 64 KB."""
        self._need6("reading memory")
        if not 0 < size <= PEEK_MAX:
            raise AmigaError(f"between 1 and {PEEK_MAX} bytes at a time")
        return self._one(Request(PEEK, f"{address & 0xFFFFFFFF:08x}", 0, 0, (size >> 16) & 0xFFFF,
                                 size & 0xFFFF, read=_read_blob))

    def tasks(self) -> list[str]:
        """TASKS lines: T address name command type pri state stack_size stack_used sigwait sigrecvd."""
        self._need6("listing tasks")
        return self._text(TASKS)

    def task_control(self, address: int, op: int, value: int) -> None:
        """op 1: priority value; op 2: break signals value (1 C, 2 D, 4 E, 8 F)."""
        self._need6("changing a task")
        self._one(Request(TASKCTL, f"{address & 0xFFFFFFFF:08x}", op, value & 0xFFFF))

    def poke(self, address: int, data: bytes) -> None:
        """Write memory; amigentd refuses unless started with ALLOWPOKE."""
        self._need6("writing memory")
        if not 0 < len(data) <= PEEK_MAX:
            raise AmigaError(f"between 1 and {PEEK_MAX} bytes at a time")
        self._one(Request(POKE, f"{address & 0xFFFFFFFF:08x}", 0, 0, len(data) >> 16,
                          len(data) & 0xFFFF, data))

    def trace(self, op: int, address: int = 0) -> list[str]:
        """op 1 set a tracepoint, 2 list them (text), 3 clear all."""
        self._need6("tracepoints")
        path = f"{address & 0xFFFFFFFF:08x}" if op == 1 else ""
        if op == 2:
            return self._one(Request(TRACE, path, op, read=_read_text))
        self._one(Request(TRACE, path, op))
        return []

    def _need_probe(self, what: str) -> None:
        self._need6(what)
        if getattr(self, "_probe", None) is None:
            self._probe = self.sysinfo().get("probe") == "1"
        if not self._probe:
            raise AmigaError(f"{what} needs a newer amigentd 1.4.0 build; run amigent-setup --update")

    def locks(self, screen: int = 0) -> list[str]:
        """K lines: kind address what owner nest queue waiters, for a screen's layers."""
        self._need_probe("looking at locks")
        return self._one(Request(LOCKS, f"{screen & 0xFFFFFFFF:08x}", 0, read=_read_text))

    def cpu(self, seconds: int, every_ms: int = 10) -> list[str]:
        """N and C lines: what ran, sampled on the Amiga for seconds."""
        self._need_probe("measuring CPU load")
        return self._one(Request(CPU, "", 0, seconds, every_ms, read=_read_text, wait=seconds + 20))

    def drag_measure(self, window: int, pattern: int, steps: int, step: int) -> list[str]:
        """D lines, one per step of dragging the window by its title bar."""
        self._need_probe("measuring window dragging")
        return self._one(Request(DRAG, f"{window & 0xFFFFFFFF:08x}", pattern, steps, step,
                                 read=_read_text, wait=steps / 10 + 30))

    def peek_many(self, spans: list[tuple[int, int]]) -> list:
        """Several PEEKs on one wire: bytes, or the AmigaError, for each (address, size)."""
        self._need6("reading memory")
        return self.pipeline([Request(PEEK, f"{a & 0xFFFFFFFF:08x}", 0, 0, (n >> 16) & 0xFFFF, n & 0xFFFF,
                                      read=_read_blob) for a, n in spans])

    def quiet(self, seconds: int) -> None:
        """amigent-monitor draws nothing for seconds (0: again now)."""
        self._need_probe("a quiet monitor")
        self._one(Request(QUIET, "", 0, seconds))

    def _apollo_opcode(self, what: str, feature: str) -> int:
        """48 on protocol 6 when audio=1. Protocol 5 uses 38 only when advertised.

        A protocol 6 channel is never sent opcode 38 for AUDIO. A classic daemon
        that did not advertise the extension is not sent one. PIP has no method.
        """
        if not self._sock:
            self.connect()
        if self.protocol < PACK_PROTOCOL:
            raise AmigaError(f"{what} needs amigentd 1.3.0 or newer (protocol {PACK_PROTOCOL}); "
                             "run amigent-setup --update")
        opcode = feature_opcode(self.protocol, feature, self.sysinfo())
        if opcode is None:
            raise AmigaError(f"{what} needs the Apollo build of amigentd")
        return opcode

    def audio(self, ms: int = 0) -> tuple[int, bytes]:
        """One shot of the AHI mix arne.audio is playing: (sample rate, s16le stereo).

        ms is how long to collect, up to a fifth of a second. Zero returns the
        newest finished half. Empty when nothing is playing through AHI.
        """
        self._request(self._apollo_opcode("ARNE audio", "AUDIO"), y=max(0, min(int(ms), 200)))
        head = self._exact(8)
        rate, size = struct.unpack(">II", head)
        return rate, self._exact(size) if size else b""

    def audio_start(self) -> None:
        """Ask the daemon to copy the mix into its FIFO and push it until audio_stop."""
        self._audio_opcode = self._apollo_opcode("ARNE audio", "AUDIO")
        self._request(self._audio_opcode, arg=1)

    def audio_read(self) -> tuple[int, bytes] | None:
        """The next streamed chunk, or None when the daemon has ended the stream."""
        rate, size = struct.unpack(">II", self._exact(8))
        if size == 0xFFFFFFFF:
            return None
        if size > 256 * 1024:
            raise AmigaError("audio chunk is too large")
        return rate, self._exact(size) if size else b""

    def audio_stop(self) -> None:
        """Tell the daemon to finish the stream. The end marker arrives via audio_read."""
        if not self._sock:
            return
        self._sock.settimeout(self.timeout)
        opcode = self._audio_opcode or self._apollo_opcode("ARNE audio", "AUDIO")
        self._header(opcode, arg=2)

    def debug_log(self, since: int = 0) -> tuple[int, bytes]:
        """What went through RawPutChar since the count since: (count now, bytes)."""
        self._need5("reading debug output")
        return self._one(Request(DEBUGLOG, "", 0, 0, (since >> 16) & 0xFFFF, since & 0xFFFF,
                                 read=_read_debug))


class Commands:
    """amigentd's command channel when the Amiga offers it, else amigactld."""

    def __init__(self, host: str, ctl_port: int = 6800, screen_port: int = 6801,
                 timeout: float = 30.0):
        self.host, self.ctl_port, self.screen_port, self.timeout = host, ctl_port, screen_port, timeout
        self.backend: AmigaChannel | AmigaCtl | None = None

    def connect_audio(self, *, tls: bool) -> None:
        """The music stream. A TLS failure stays a failure and does not fall through to amigactld."""
        channel = AmigaChannel(self.host, self.screen_port, self.timeout)
        channel.connect()
        if tls and not hasattr(channel._sock, "version"):
            channel.close()
            raise AmigaError(PLAIN_CLOSED_TEXT)
        self.backend = channel

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
