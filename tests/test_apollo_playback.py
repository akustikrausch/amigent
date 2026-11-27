"""Viewer playback and amiga_listen. Nothing here contacts an Amiga or plays a sample."""

import struct

import pytest

import amigent.server as server
from amigent.channel import (
    PLAIN_CLOSED, PLAIN_CLOSED_TEXT, Commands, audio_uses_tls, feature_opcode, pcm_wav,
)
from amigent.errors import AmigaError
from amigent.viewer import Viewer, pcm_message
from amigent.webserver import WEB


def test_audio_capability_follows_the_advertisement_and_the_protocol():
    assert feature_opcode(6, "AUDIO", {"audio": "49"}) == 49
    assert feature_opcode(6, "AUDIO", {"audio": "1"}) is None
    assert feature_opcode(6, "AUDIO", {}) is None
    assert feature_opcode(6, "AUDIO", {"audio": "0"}) is None
    assert feature_opcode(5, "AUDIO", {"audio": "49"}) is None
    assert feature_opcode(4, "AUDIO", {"audio": "49"}) is None
    viewer = (WEB.parent / "viewer.py").read_text(encoding="utf-8")
    page = (WEB / "viewer.js").read_text(encoding="utf-8")
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
    info = {"os": "ApolloOS", "cpu": "68080", "audio": "49", "daemon_protocol": 6}
    info.update(extra)
    return info


class Page:
    """An open page: what the viewer sent it."""

    def __init__(self):
        self.got = []

    def send(self, data):
        self.got.append(data)


def _page(host="192.0.2.10", flags=0):
    viewer = Viewer(host, screen_port=1, ctl_port=1)
    viewer.screen.flags = flags
    viewer.online = True
    viewer.page = Page()
    viewer.clients.add(viewer.page)
    viewer.sent = []
    viewer.broadcast = lambda data: viewer.sent.append(data)
    viewer.event = lambda kind, **fields: viewer.sent.append((kind, fields))
    return viewer


def test_playback_starts_only_when_asked_and_stops_cleanly():
    viewer = _page(flags=PLAIN_CLOSED)
    viewer.amiga = _apollo_audio()
    assert not viewer._hearing()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer._hearing() and viewer._audio_ready()
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00"), None]
    viewer._play_audio(link)
    assert link.tls is True and link.started == 1 and link.stopped == 1 and link.closed == 1
    assert viewer.hear is False
    assert viewer.sent[0] == ("audio", {"on": True})
    assert not [m for m in viewer.sent if isinstance(m, (bytes, bytearray))]  # PCM is not broadcast
    frame = next(m for m in viewer.page.got if isinstance(m, (bytes, bytearray)))
    assert frame == pcm_message(48000, b"\x01\x00\x02\x00")
    assert frame[0] == 5 and struct.unpack(">I", frame[1:5]) == (48000,)
    assert ("audio", {"on": False}) in viewer.sent

    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link2 = Link()
    link2.reads = [(48000, b"\x00\x00\x00\x00"), None]
    viewer._play_audio(link2)
    assert link2.started == 1 and link2.stopped == 1 and viewer.hear is False


def test_unsupported_audio_and_a_lost_connection_leave_no_stream():
    viewer = _page()
    viewer.amiga = {}
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert not viewer._hearing() and not viewer._audio_ready()
    assert ("audio", {"on": False}) in viewer.sent
    viewer.amiga = {"os": "AmigaOS", "audio": ""}
    assert not viewer._audio_ready() and not viewer.hear

    viewer.amiga = _apollo_audio(audio="1")  # legacy fork opcode 48 must be refused
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert not viewer._hearing() and not viewer._audio_ready()

    viewer.amiga = _apollo_audio()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [AmigaError("the Amiga does not answer")]
    with pytest.raises(AmigaError):
        viewer._play_audio(link)
    assert not viewer.hear and link.closed == 1

    odd = Link()
    viewer.hearers.add(viewer.page)
    odd.reads = [(48000, b"\x00\x00\x00")]
    with pytest.raises(AmigaError, match="stereo"):
        viewer._play_audio(odd)


def test_reconnection_does_not_keep_playback():
    viewer = _page()
    viewer.amiga = _apollo_audio()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    viewer._set_online(False, "the Amiga does not answer")
    assert viewer.hear is False and viewer.amiga == {}
    viewer._set_online(True)
    assert viewer.hear is False and not viewer._hearing()
    client = next(iter(viewer.clients))
    viewer.hearers.add(client)
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

    monkeypatch.setattr(server._session(), "ctl", Quiet())
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    assert server.amiga_listen(1) == ["this Amiga has no AHI mix to hear"]

    class LegacyFork:
        def sysinfo(self):
            return {"audio": "1"}

        def audio(self, ms):
            raise AssertionError("legacy audio=1 must not be called")

    monkeypatch.setattr(server._session(), "ctl", LegacyFork())
    legacy_reply = server.amiga_listen(1)
    assert len(legacy_reply) == 1 and "legacy audio=1" in legacy_reply[0] and "49" in legacy_reply[0]

    class Down:
        def sysinfo(self):
            return {"audio": "49"}

        def audio(self, ms):
            raise AmigaError("the Amiga does not answer")

    monkeypatch.setattr(server._session(), "ctl", Down())
    with pytest.raises(AmigaError, match="does not answer"):
        server.amiga_listen(1)

    pcm = b"\x00\x00" * int(0.2 * 48000 * 2)

    class Playing:
        def sysinfo(self):
            return {"audio": "49"}

        def audio(self, ms):
            assert ms == 200
            return 48000, pcm

    monkeypatch.setattr(server._session(), "ctl", Playing())
    text, block = server.amiga_listen(0.2)
    assert text == f"heard {len(pcm)} bytes at 48000 Hz"
    assert block.data == pcm_wav(pcm, 48000) and block.data.startswith(b"RIFF")

    clock = {"t": 0.0}
    monkeypatch.setattr(server.time, "monotonic", lambda: clock.__setitem__("t", clock["t"] + 1) or clock["t"])
    monkeypatch.setattr(server.time, "sleep", lambda _seconds: None)

    class Silent:
        def sysinfo(self):
            return {"audio": "49"}

        def audio(self, ms):
            return 48000, b""

    monkeypatch.setattr(server._session(), "ctl", Silent())
    assert server.amiga_listen(1) == ["nothing is playing through AHI"]


def test_listen_on_amigactld_says_so_and_on_amigentd_still_listens(monkeypatch):
    from amigent.amigactl import AmigaCtl
    from amigent.channel import AmigaChannel

    class OldDaemon(AmigaCtl):
        def sysinfo(self):
            return {"audio": "49"}  # even when it claims the mix, it has no CMD_AUDIO

    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    old = Commands("192.0.2.1")
    old.backend = OldDaemon("192.0.2.1")
    monkeypatch.setattr(server._session(), "ctl", old)
    reply = server.amiga_listen(1)
    assert len(reply) == 1 and "amigactld" in reply[0] and "amigent-setup --update" in reply[0]

    pcm = b"\x01\x00\x02\x00" * 9600
    channel = AmigaChannel.__new__(AmigaChannel)
    channel.sysinfo = lambda: {"audio": "49"}
    channel.audio = lambda ms: (48000, pcm)
    new = Commands("192.0.2.1")
    new.backend = channel
    monkeypatch.setattr(server._session(), "ctl", new)
    text, block = server.amiga_listen(0.2)
    assert text == f"heard {len(pcm)} bytes at 48000 Hz" and block.data == pcm_wav(pcm, 48000)


def test_apollo_source_does_not_write_stream_diagnostics():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    apollo = (root / "src/native/amiga" / "apollo" / "apollo_audio.c").read_text(encoding="utf-8")
    assert 'Open("T:audio-mem"' not in apollo
    assert 'Open("T:audio-ptrs"' not in apollo
    assert "dump_mix" not in apollo
    assert "MODE_NEWFILE" not in apollo
    assert "apollo_audio_claim" in apollo and "apollo_audio_release" in apollo
    assert "#define AUDIO_FIFO (256 * 1024)" in apollo


def test_stream_chunk_bounds_and_oneshot_framing_on_client():
    from amigent.channel import AmigaChannel

    channel = AmigaChannel.__new__(AmigaChannel)
    channel.protocol = 6
    channel._sock = type("Sock", (), {"settimeout": lambda self, t: None})()
    channel.timeout = 30
    channel._audio_opcode = 49
    channel.sysinfo = lambda: {"audio": "49"}

    sent = []
    channel._request = lambda cmd, **kw: sent.append((cmd, kw))
    empty = iter([struct.pack(">II", 0, 0)])
    channel._exact = lambda n: next(empty)
    rate, pcm = channel.audio(0)
    assert sent == [(49, {"y": 0})] and rate == 0 and pcm == b""

    channel.audio_start()
    assert sent[-1] == (49, {"arg": 1})
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
    assert headers == [(49, 2)]
