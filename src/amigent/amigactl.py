"""Client for the amigactld wire protocol: files, commands, system state. The old-school path.

Clean-room job from the published spec (docs/protocol.md and
docs/protocol-commands.md in the amigactl project), not a rip of its client code.
"""

from __future__ import annotations

import socket

from .client import ExecResult, VolumeFlush, join_path
from .errors import AmigaError

__all__ = ["AmigaCtl", "AmigaError", "ExecResult", "join_path"]  # the old addresses still deliver

ENCODING = "iso-8859-1"
CHUNK_SIZE = 4096
MAX_LINE = 4096


class AmigaCtl(VolumeFlush):
    def __init__(self, host: str, port: int = 6800, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self.banner = ""

    def connect(self) -> None:
        self.close()
        try:
            from .machines import address
            self._sock = socket.create_connection((address(self.host), self.port), self.timeout)
            self.banner = self._line()
        except OSError as e:
            self.close()
            raise AmigaError(f"cannot reach amigactld at {self.host}:{self.port}: {e}") from e
        if not self.banner.startswith("AMIGACTL "):
            banner, self.banner = self.banner, ""
            self.close()
            raise AmigaError(f"unexpected banner: {banner!r}")

    def close(self) -> None:
        if self._sock:
            self._sock.close()
        self._sock = None
        self._buf = b""

    # framing

    def _recv(self) -> None:
        assert self._sock
        data = self._sock.recv(65536)
        if not data:
            raise AmigaError("connection closed by daemon")
        self._buf += data

    def _line(self) -> str:
        while b"\n" not in self._buf:
            self._recv()
        raw, self._buf = self._buf.split(b"\n", 1)
        return raw.rstrip(b"\r").decode(ENCODING)

    def _exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            self._recv()
        data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def _send_line(self, line: str) -> None:
        raw = line.encode(ENCODING) + b"\n"
        if len(raw) - 1 > MAX_LINE or "\n" in line:
            raise AmigaError("command line too long or contains a newline")
        if not self._sock:
            self.connect()
        assert self._sock
        self._sock.sendall(raw)

    def _status(self, line: str) -> str:
        """The info field of an OK line; on ERR eat the sentinel and raise."""
        if line == "OK" or line.startswith("OK "):
            return line[3:]
        if line.startswith("ERR "):
            parts = line.split(" ", 2)
            self._line()  # sentinel
            code = int(parts[1]) if parts[1].isdigit() else None
            raise AmigaError(parts[2] if len(parts) > 2 else line, code)
        raise AmigaError(f"malformed response: {line!r}")

    def _text(self, command: str, *extra_lines: str) -> tuple[str, list[str]]:
        """Run a command whose answer is dot-stuffed lines, SMTP style, ended by '.'."""
        self._send_line(command)
        for line in extra_lines:
            self._send_line(line)
        info = self._status(self._line())
        lines = []
        while True:
            line = self._line()
            if line == ".":
                return info, lines
            lines.append(line[1:] if line.startswith("..") else line)

    def _binary(self, command: str, timeout: float | None = None) -> tuple[str, bytes]:
        """Run a command whose answer comes in DATA chunks until END."""
        self._send_line(command)
        assert self._sock
        self._sock.settimeout(timeout if timeout is not None else self.timeout)
        try:
            info = self._status(self._line())
            chunks = []
            while True:
                line = self._line()
                if line == "END":
                    break
                if not line.startswith("DATA "):
                    raise AmigaError(f"malformed chunk header: {line!r}")
                chunks.append(self._exact(int(line[5:])))
            if self._line() != ".":
                raise AmigaError("missing sentinel")
            return info, b"".join(chunks)
        finally:
            if self._sock:
                self._sock.settimeout(self.timeout)

    # files

    def list_dir(self, path: str, recursive: bool = False) -> list[dict]:
        _, lines = self._text(f"DIR {path}" + (" RECURSIVE" if recursive else ""))
        entries = []
        for line in lines:
            kind, name, size, protection, stamp = line.split("\t")
            entries.append(
                {"type": kind, "name": name, "size": int(size), "protection": protection, "date": stamp}
            )
        return entries

    def stat(self, path: str) -> dict:
        _, lines = self._text(f"STAT {path}")
        return dict(line.split("=", 1) for line in lines)

    def read_file(self, path: str) -> bytes:
        return self._binary(f"READ {path}")[1]

    def write_file(self, path: str, data: bytes, append: bool = False) -> None:
        self._send_line(f"{'APPEND' if append else 'WRITE'} {path} {len(data)}")
        line = self._line()
        if line != "READY":
            self._status(line)
            raise AmigaError(f"expected READY, got {line!r}")
        assert self._sock
        for i in range(0, len(data), CHUNK_SIZE):
            chunk = data[i : i + CHUNK_SIZE]
            self._sock.sendall(f"DATA {len(chunk)}\n".encode() + chunk)
        self._sock.sendall(b"END\n")
        self._status(self._line())
        if self._line() != ".":
            raise AmigaError("missing sentinel")

    def checksum(self, path: str, size: int = 0) -> int:  # size: AmigaChannel uses it for patience
        return int(dict(line.split("=", 1) for line in self._text(f"CHECKSUM {path}")[1])["crc32"], 16)

    def delete(self, path: str) -> None:
        self._text(f"DELETE {path}")

    def rename(self, old: str, new: str) -> None:
        self._text("RENAME", old, new)

    def copy(self, source: str, dest: str) -> None:
        self._text("COPY", source, dest)

    def makedir(self, path: str) -> None:
        self._text(f"MAKEDIR {path}")

    # commands

    def exec(self, command: str, cd: str | None = None, timeout: float | None = None) -> ExecResult:
        """Run synchronously. The daemon is single-tasking here: everyone waits till it returns."""
        prefix = f"CD={cd} " if cd else ""
        info, data = self._binary(f"EXEC {prefix}{command}", timeout)
        return ExecResult(int(info.split("=", 1)[1]), data.decode(ENCODING))

    def exec_async(self, command: str) -> int:
        """Fire and forget, no output captured; returns the daemon's process id."""
        return int(self._text(f"EXEC ASYNC {command}")[0])

    # system state

    def sysinfo(self) -> dict:
        _, lines = self._text("SYSINFO")
        return dict(line.split("=", 1) for line in lines)

    def volumes(self) -> list[dict]:
        _, lines = self._text("VOLUMES")
        keys = ("name", "used", "free", "capacity", "blocksize")
        return [
            {k: (v if k == "name" else int(v)) for k, v in zip(keys, line.split("\t"))}
            for line in lines
        ]
