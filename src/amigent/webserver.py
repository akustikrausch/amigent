"""The viewer's plumbing: HTTP for the page and its files, RFC 6455 for the live link.

Nothing here knows about Amigas; _Handler talks to a viewer object that has
token, port, join/leave/submit and the file calls.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .amigactl import AmigaError

WEB = Path(__file__).with_name("web")
STATIC = {"viewer.js": "text/javascript", "viewer.css": "text/css", "icon.svg": "image/svg+xml",
          "icon.png": "image/png", "amigent.ico": "image/x-icon"}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_UPLOAD = 512 * 2**20
MAX_MESSAGE = 1 << 20
SEND_TIMEOUT = 5  # a page that stops reading must not hold the screen lock


class WebSocket:
    """Just enough RFC 6455 for one local page: unfragmented sends, masked receives."""

    def __init__(self, reader, sock: socket.socket):
        self.reader = reader
        self.sock = sock
        self._send_lock = threading.Lock()

    @staticmethod
    def accept_key(key: str) -> str:
        return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()

    def send(self, data: bytes | str, opcode: int | None = None) -> None:
        if isinstance(data, str):
            data, opcode = data.encode(), opcode or 1
        n = len(data)
        head = bytes([0x80 | (opcode or 2)])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += struct.pack(">BH", 126, n)
        else:
            head += struct.pack(">BQ", 127, n)
        with self._send_lock:
            self.sock.sendall(head + data)

    def receive(self) -> str | bytes | None:
        """Next message, or None when the page closed the socket."""
        message, kind = b"", None
        while True:
            b1, b2 = self._exact(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            if n > MAX_MESSAGE:
                raise EOFError  # nothing the page sends is that large
            mask = self._exact(4) if b2 & 0x80 else b""
            payload = self._exact(n)
            if mask and n:
                key = (mask * (n // 4 + 1))[:n]
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
            opcode = b1 & 0x0F
            if opcode == 8:
                return None
            if opcode == 9:
                self.send(payload, 10)
                continue
            if opcode == 10:
                continue
            kind = kind or opcode
            message += payload
            if b1 & 0x80:
                return message.decode("utf-8", "replace") if kind == 1 else message

    def _exact(self, n: int) -> bytes:
        data = self.reader.read(n) if n else b""
        if len(data) != n:
            raise EOFError
        return data

    def close(self) -> None:
        try:
            self.send(b"", 8)
        except OSError:
            pass



class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        pass  # a page that closes mid-request is no news


_Server = QuietServer  # the old name, kept for old imports


class TokenHandler(BaseHTTPRequestHandler):
    """The bouncer every local page shares: right Host, right token, or no entry.

    Subclasses set `gate`, anything with a `port` and a `token`, and may add
    their own files to `extra_static`."""

    gate = None
    extra_static: dict[str, str] = {}
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass  # stdout belongs to MCP, and nobody needs the noise on stderr

    def _allowed(self, query: dict) -> bool:
        port = self.gate.port
        host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
        return host_ok and secrets.compare_digest(query.get("t", [""])[0], self.gate.token)

    def _reply(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, value) -> None:
        self._reply(code, json.dumps(value).encode())

    def _static(self, name: str) -> bool:
        """Serve the page's own files and languages to anyone; True if that was the job."""
        if name in STATIC or name in self.extra_static:
            self._reply(200, (WEB / name).read_bytes(), STATIC.get(name) or self.extra_static[name])
            return True
        if re.fullmatch(r"locales/[a-z]{2}\.json", name) and (WEB / name).is_file():
            self._reply(200, (WEB / name).read_bytes(), "application/json; charset=utf-8")
            return True
        return False


class _Handler(TokenHandler):
    viewer: "Viewer"  # noqa: F821 - set by the viewer, which imports us

    @property
    def gate(self):
        return self.viewer

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if self._static(url.path.lstrip("/")):
            return
        if not self._allowed(query):
            return self._reply(403, b"Open the viewer through amigent.", "text/plain")
        if url.path == "/":
            return self._reply(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/ws":
            return self._websocket()
        if url.path == "/api/folders":
            try:
                return self._json(200, self.viewer.folders(query.get("path", [""])[0]))
            except (AmigaError, OSError, ValueError) as e:
                return self._json(502, {"error": str(e)})
        if url.path == "/api/download":
            path = query.get("path", [""])[0]
            try:
                data = self.viewer.download(path)
            except (AmigaError, OSError, ValueError) as e:
                return self._json(502, {"error": str(e)})
            name = re.split(r"[:/]", path)[-1] or "file"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if url.path == "/api/clients":
            with self.viewer.clients_lock:
                return self._json(200, {"clients": len(self.viewer.clients)})
        if url.path == "/api/clipboard":
            try:
                return self._json(200, {"text": self.viewer.read_clipboard()})
            except (AmigaError, OSError, ValueError) as e:
                return self._json(502, {"error": str(e)})
        self._reply(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._allowed(query) or url.path not in ("/api/upload", "/api/clipboard", "/api/address"):
            return self._reply(403, b"forbidden", "text/plain")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = -1
        if size < 0:
            return self._json(400, {"error": "length missing"})
        if size > (MAX_UPLOAD if url.path == "/api/upload" else MAX_MESSAGE):
            return self._json(413, {"error": "file too large"})
        data = self.rfile.read(size)
        if url.path == "/api/address":
            try:
                self.viewer.set_address(data.decode("utf-8", "replace"))
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {})
        if url.path == "/api/clipboard":
            try:
                self.viewer.write_clipboard(data.decode("utf-8", "replace"))
            except (AmigaError, OSError, ValueError) as e:
                return self._json(502, {"error": str(e)})
            return self._json(200, {})
        try:
            job = self.viewer.queue_upload(query.get("folder", ["RAM:"])[0],
                                           query.get("name", ["file"])[0], data,
                                           query.get("overwrite", ["0"])[0] == "1")
        except (AmigaError, OSError) as e:
            return self._json(502, {"error": str(e)})
        self._json(409 if job.get("exists") else 200, job)

    def _websocket(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in self.headers.get("Upgrade", "").lower():
            return self._reply(400, b"websocket expected", "text/plain")
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", WebSocket.accept_key(key))
        self.end_headers()
        self.wfile.flush()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:  # sends give up instead of waiting forever for a page that stopped reading
            if sys.platform == "win32":
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, SEND_TIMEOUT * 1000)
            else:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                                           struct.pack("ll", SEND_TIMEOUT, 0))
        except OSError:
            pass
        ws = WebSocket(self.rfile, self.connection)
        try:
            self.viewer.join(ws)
            while (message := ws.receive()) is not None:
                if isinstance(message, str):
                    try:
                        self.viewer.submit(json.loads(message))
                    except ValueError:
                        pass
        except (OSError, EOFError):
            pass
        finally:
            self.viewer.leave(ws)
            ws.close()
            self.close_connection = True
