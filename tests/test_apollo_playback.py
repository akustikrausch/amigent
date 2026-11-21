"""Viewer playback and amiga_listen. Nothing here contacts an Amiga or plays a sample.

The protected daemon's bytes stay the ones already packaged. A passing test
means the PC accepted framed audio, not that the mix was heard on hardware.
"""

import hashlib
import struct

import pytest

import amigent.server as server
from amigent.channel import (
    PLAIN_CLOSED, PLAIN_CLOSED_TEXT, AmigaChannel, Commands, audio_uses_tls, feature_opcode,
    pcm_wav,
)
from amigent.errors import AmigaError
from amigent.paths import AMIGA
from amigent.viewer import Viewer, pcm_message
from amigent.webserver import WEB

DAEMON = (128744, "63db83ff021c47f7146e0872cd36bf90eb201f36a0cf455a58f3c518a84e8ef1")  # HW-validated D4/D5/D5B on Apollo V4-SA


def test_audio_capability_follows_the_advertisement_and_the_protocol():
    assert feature_opcode(6, "AUDIO", {"audio": "1"}) == 48
    assert feature_opcode(6, "AUDIO", {}) is None
    assert feature_opcode(6, "AUDIO", {"audio": "0"}) is None
    assert feature_opcode(5, "AUDIO", {"audio": "1"}) == 38
    assert feature_opcode(5, "AUDIO", {}) is None
    assert feature_opcode(4, "AUDIO", {"audio": "1"}) is None
    viewer = (WEB.parent / "viewer.py").read_text(encoding="utf-8")
    page = (WEB / "viewer.js").read_text(encoding="utf-8")
    # This contribution ships Hear only; PIP surface polling stays out of scope.
    assert "_poll_pip" not in viewer and "drawPip" not in page
    assert "hear_eligible" in viewer and "showHear" in page and "kind === 5" in page


def test_remote_tlsonly_audio_does_not_accept_plaintext(monkeypatch):
    assert audio_uses_tls(PLAIN_CLOSED, "192.0.2.10")
    assert not audio_uses_tls(PLAIN_CLOSED, "127.0.0.1")
    assert not audio_uses_tls(PLAIN_CLOSED, "localhost")
    assert not audio_uses_tls(PLAIN_CLOSED, "::1")
    assert not audio_uses_tls(0, "192.0.2.10")

    class Plain:
        closed = False

        def close(self):
            self.closed = True

    class Encrypted(Plain):
        def version(self):
            return "TLSv1.3"

    made = []

    class FakeChannel:
        def __init__(self, host, port, timeout):
            self.host, self.port, self.timeout = host, port, timeout
            self._sock = Encrypted() if host == "tls" else Plain()
            self.closed = False
            made.append(self)

        def connect(self):
            if self.host == "down":
                raise AmigaError(PLAIN_CLOSED_TEXT)

        def close(self):
            self.closed = True
            self._sock.close()

    monkeypatch.setattr("amigent.channel.AmigaChannel", FakeChannel)
    monkeypatch.setattr("amigent.channel.AmigaCtl", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("audio fell through to amigactld")))

    refused = Commands("192.0.2.10")
    with pytest.raises(AmigaError, match="only encrypted"):
        refused.connect_audio(tls=True)
    assert refused.backend is None and made[-1].closed

    failed = Commands("down")
    with pytest.raises(AmigaError, match="only encrypted"):
        failed.connect_audio(tls=True)
    assert failed.backend is None

    accepted = Commands("tls")
    accepted.connect_audio(tls=True)
    assert accepted.backend is made[-1] and accepted.is_tls


class Link:
    def __init__(self):
        self.connected = False
        self.tls = None
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.reads = []

    def connect_audio(self, *, tls):
        self.tls = tls
        self.connected = True

    def audio_start(self):
        self.started += 1

    def audio_read(self):
        if not self.reads:
            return None
        item = self.reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def audio_stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1
        self.connected = False


def _apollo_audio(**extra):
    info = {"os": "ApolloOS", "cpu": "68080", "audio": "1", "daemon_protocol": 6}
    info.update(extra)
    return info


def _page(host="192.0.2.10", flags=0):
    viewer = Viewer(host, screen_port=1, ctl_port=1)
    viewer.screen.flags = flags
    viewer.online = True
    viewer.clients.add(object())
    viewer.sent = []
    viewer.broadcast = lambda data: viewer.sent.append(data)
    viewer.event = lambda kind, **fields: viewer.sent.append((kind, fields))
    return viewer


def test_playback_starts_only_when_asked_and_stops_cleanly():
    viewer = _page(flags=PLAIN_CLOSED)
    viewer.amiga = _apollo_audio()
    assert not viewer._hearing()
    viewer.submit({"t": "audio", "on": True})
    assert viewer._hearing() and viewer._audio_ready()
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00"), None]
    viewer._play_audio(link)
    assert link.tls is True and link.started == 1 and link.stopped == 1 and link.closed == 1
    assert viewer.hear is False
    assert viewer.sent[0] == ("audio", {"on": True})
    frame = next(m for m in viewer.sent if isinstance(m, (bytes, bytearray)))
    assert frame == pcm_message(48000, b"\x01\x00\x02\x00")
    assert frame[0] == 5 and struct.unpack(">I", frame[1:5]) == (48000,)
    assert ("audio", {"on": False}) in viewer.sent

    # User-initiated second Hear after cleanup
    viewer.submit({"t": "audio", "on": True})
    link2 = Link()
    link2.reads = [(48000, b"\x00\x00\x00\x00"), None]
    viewer._play_audio(link2)
    assert link2.started == 1 and link2.stopped == 1 and viewer.hear is False


def test_unsupported_audio_and_a_lost_connection_leave_no_stream():
    viewer = _page()
    viewer.amiga = {}
    viewer.submit({"t": "audio", "on": True})
    # Empty / ineligible capabilities never arm Hear.
    assert not viewer._hearing() and not viewer._audio_ready()
    assert ("audio", {"on": False}) in viewer.sent
    viewer.amiga = {"os": "AmigaOS", "audio": ""}
    assert not viewer._audio_ready() and not viewer.hear

    viewer.amiga = _apollo_audio()
    viewer.submit({"t": "audio", "on": True})
    link = Link()
    link.reads = [AmigaError("the Amiga does not answer")]
    with pytest.raises(AmigaError):
        viewer._play_audio(link)
    assert not viewer.hear and link.closed == 1

    odd = Link()
    viewer.hear = True
    odd.reads = [(48000, b"\x00\x00\x00")]
    with pytest.raises(AmigaError, match="stereo"):
        viewer._play_audio(odd)


def test_reconnection_does_not_keep_playback():
    viewer = _page()
    viewer.amiga = _apollo_audio()
    viewer.submit({"t": "audio", "on": True})
    viewer._set_online(False, "the Amiga does not answer")
    assert viewer.hear is False and viewer.amiga == {}
    viewer._set_online(True)
    assert viewer.hear is False and not viewer._hearing()
    client = next(iter(viewer.clients))
    viewer.hear = True
    viewer.leave(client)
    assert viewer.hear is False and not viewer.clients


def test_the_page_arms_playback_from_the_button_and_keeps_the_screen_path():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "viewer.js").read_text(encoding="utf-8")
    assert 'id="hear"' in html and 'id="screen"' in html
    assert 'send({ t: "audio", on: true })' in js
    assert 'send({ t: "audio", on: false })' in js
    assert "amigent-pcm" in js and "kind === 5" in js
    assert "function drawTiles" in js and "function onBinary" in js
    assert "hearEligible" in js
    connect = js.split("function connect()", 1)[1].split("function showState()", 1)[0]
    assert 't: "audio"' not in connect


def test_listen_validates_and_reports_transport_results(monkeypatch):
    for bad in (True, "1", None, 0, 0.1, 3):
        with pytest.raises(AmigaError, match="0.2 to 2"):
            server._listen_seconds(bad)
    assert server._listen_seconds(0.2) == 0.2
    assert server._listen_seconds(2) == 2

    class Quiet:
        def sysinfo(self):
            return {"audio": "0"}

        def audio(self, ms):
            raise AssertionError("the mix was read")

    monkeypatch.setattr(server, "ctl", Quiet())
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    assert server.amiga_listen(1) == ["this Amiga has no AHI mix to hear"]

    class Down:
        def sysinfo(self):
            return {"audio": "1"}

        def audio(self, ms):
            raise AmigaError("the Amiga does not answer")

    monkeypatch.setattr(server, "ctl", Down())
    with pytest.raises(AmigaError, match="does not answer"):
        server.amiga_listen(1)

    pcm = b"\x00\x00" * int(0.2 * 48000 * 2)

    class Playing:
        def sysinfo(self):
            return {"audio": "1"}

        def audio(self, ms):
            assert ms == 200
            return 48000, pcm

    monkeypatch.setattr(server, "ctl", Playing())
    text, block = server.amiga_listen(0.2)
    assert text == f"heard {len(pcm)} bytes at 48000 Hz"
    assert block.data == pcm_wav(pcm, 48000) and block.data.startswith(b"RIFF")

    clock = {"t": 0.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock.__setitem__("t", clock["t"] + 1) or clock["t"])
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    class Silent:
        def sysinfo(self):
            return {"audio": "1"}

        def audio(self, ms):
            return 48000, b""

    monkeypatch.setattr(server, "ctl", Silent())
    assert server.amiga_listen(1) == ["nothing is playing through AHI"]


def test_the_packaged_apollo_daemon_is_the_stream_safe_build():
    data = (AMIGA / "bin" / "amigentd.apollo").read_bytes()
    assert len(data) == DAEMON[0]
    assert hashlib.sha256(data).hexdigest() == DAEMON[1]


def test_fork_apollo_source_does_not_write_stream_diagnostics():
    """Software check only — not hardware evidence. Packaged F-DAE bytes stay above."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    apollo = (root / "amiga" / "apollo" / "apollo_audio.c").read_text(encoding="utf-8")
    assert 'Open("T:audio-mem"' not in apollo
    assert 'Open("T:audio-ptrs"' not in apollo
    assert "dump_mix" not in apollo
    assert "MODE_NEWFILE" not in apollo
    assert "apollo_audio_claim" in apollo and "apollo_audio_release" in apollo
    assert "#define AUDIO_FIFO (256 * 1024)" in apollo


def test_stream_chunk_bounds_and_oneshot_framing_on_fork_client():
    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = type("Sock", (), {"settimeout": lambda self, t: None})()
    channel.timeout = 30
    channel._audio_opcode = 48
    channel._info = {"audio": "1"}
    channel.sysinfo = lambda: {"audio": "1"}

    sent = []
    channel._request = lambda cmd, **kw: sent.append((cmd, kw))
    empty = iter([struct.pack(">II", 0, 0)])
    channel._exact = lambda n: next(empty)
    rate, pcm = channel.audio(0)
    assert sent == [(48, {"y": 0})] and rate == 0 and pcm == b""

    channel.audio_start()
    assert sent[-1] == (48, {"arg": 1})
    pcm = b"\x01\x00\x02\x00" * 8
    blobs = iter([struct.pack(">II", 47931, len(pcm)), pcm])
    channel._exact = lambda n: next(blobs)
    got_rate, got = channel.audio_read()
    assert got_rate == 47931 and got == pcm and len(got) % 4 == 0

    channel._exact = lambda n: struct.pack(">II", 48000, 0)
    assert channel.audio_read() == (48000, b"")
    channel._exact = lambda n: struct.pack(">II", 48000, 0xFFFFFFFF)
    assert channel.audio_read() is None
    channel._exact = lambda n: struct.pack(">II", 48000, 256 * 1024 + 4)
    with pytest.raises(AmigaError, match="too large"):
        channel.audio_read()
    headers = []
    channel._header = lambda cmd, arg=0, **kw: headers.append((cmd, arg))
    channel.audio_stop()
    assert headers == [(48, 2)]
