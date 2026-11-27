"""Offline stock Viewer Hear lifecycle checks (not hardware evidence)."""
from __future__ import annotations

import struct

import pytest

from amigent.channel import PLAIN_CLOSED
from amigent.errors import AmigaError
from amigent.viewer import AUDIO_DRAIN_MAX, Viewer, pcm_message


class Link:
    def __init__(self):
        self.connected = False
        self.tls = None
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.reads = []
        self.read_calls = 0

    def connect_audio(self, *, tls):
        self.tls = tls
        self.connected = True

    def audio_start(self):
        self.started += 1

    def audio_read(self):
        self.read_calls += 1
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


class Page:
    """An open page: what the viewer sent it."""

    def __init__(self):
        self.got = []

    def send(self, data):
        self.got.append(data)


def _page():
    viewer = Viewer("192.0.2.10", screen_port=1, ctl_port=1)
    viewer.screen.flags = PLAIN_CLOSED
    viewer.online = True
    viewer.page = Page()
    viewer.clients.add(viewer.page)
    viewer.sent = []
    viewer.broadcast = lambda data: viewer.sent.append(data)
    viewer.event = lambda kind, **fields: viewer.sent.append((kind, fields))
    viewer.amiga = {"os": "ApolloOS", "cpu": "68080", "audio": "49", "daemon_protocol": 6}
    return viewer


def test_hear_enable_and_disable_releases_once():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00")]

    def between():
        viewer.submit({"t": "audio", "on": False}, viewer.page)
        link.reads.append((48000, b"\x03\x00\x04\x00"))

    viewer._play_audio(link, between)
    assert link.started == 1 and link.stopped == 1 and link.closed == 1
    assert viewer.hear is False
    assert ("audio", {"on": False}) in viewer.sent


def test_daemon_end_marker_clears_hear_and_stops():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00"), None]
    viewer._play_audio(link)
    assert viewer.hear is False
    assert link.started == 1 and link.stopped == 1 and link.closed == 1


def test_media_like_loop_does_not_restart_after_daemon_end():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00"), None]
    viewer._play_audio(link)
    assert not viewer._hearing()
    if viewer._hearing() and viewer._audio_ready():
        viewer._play_audio(link)
    assert link.started == 1


def test_user_can_restart_hear_after_complete_cleanup():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00"), None]
    viewer._play_audio(link)
    assert viewer.hear is False
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link2 = Link()
    link2.reads = [(48000, b"\x05\x00\x06\x00"), None]
    viewer._play_audio(link2)
    assert link2.started == 1 and viewer.hear is False


def test_tls_or_amiga_error_drop_requires_user_reclick():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [AmigaError("TLS handshake failed")]
    with pytest.raises(AmigaError):
        viewer._play_audio(link)
    assert viewer.hear is False and link.closed == 1
    assert not viewer._hearing()


def test_unsupported_audio_capability_clears_request():
    viewer = _page()
    viewer.amiga = {}
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert not viewer._hearing() and not viewer._audio_ready()
    viewer.amiga = {"os": "AmigaOS", "audio": ""}
    assert not viewer._audio_ready() and viewer.hear is False
    viewer.amiga = {"os": "ApolloOS", "cpu": "68080", "audio": "1", "daemon_protocol": 6}
    assert not viewer._audio_ready() and viewer.hear is False


def test_last_client_leave_clears_hear():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    client = next(iter(viewer.clients))
    viewer.leave(client)
    assert viewer.hear is False and not viewer._hearing()


def test_drain_is_bounded():
    viewer = _page()
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    link.reads = [(48000, b"\x01\x00\x02\x00")]

    def between():
        viewer.submit({"t": "audio", "on": False}, viewer.page)
        link.reads.extend([(48000, b"\x00\x00\x00\x00")] * (AUDIO_DRAIN_MAX + 20))

    viewer._play_audio(link, between)
    assert link.read_calls <= 1 + AUDIO_DRAIN_MAX
    assert link.stopped == 1 and link.closed == 1


def test_repeated_start_stop_is_safe():
    viewer = _page()
    for _ in range(3):
        viewer.submit({"t": "audio", "on": True}, viewer.page)
        link = Link()
        link.reads = [(48000, b"\x01\x00\x02\x00"), None]
        viewer._play_audio(link)
        assert viewer.hear is False
        viewer._drop_audio(link)
        assert link.closed >= 1


def test_protocol5_without_audio_capability():
    viewer = _page()
    viewer.amiga = {"os": "AmigaOS", "audio": "", "daemon_protocol": 5}
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert not viewer._audio_ready() and viewer.hear is False


def test_protocol5_apollo_with_audio_still_ineligible():
    viewer = _page()
    viewer.amiga = {
        "os": "ApolloOS", "cpu": "68080", "audio": "49", "daemon_protocol": 5,
    }
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert not viewer._audio_ready() and viewer.hear is False


def test_pcm_message_framing_unchanged():
    pcm = b"\x01\x00\x02\x00" * 4
    msg = pcm_message(47931, pcm)
    assert msg[0] == 5 and struct.unpack(">I", msg[1:5]) == (47931,) and msg[5:] == pcm


def test_page_stophear_closes_audiocontext_in_source():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "src/amigent/web/viewer.js").read_text(encoding="utf-8")
    assert "ctx.close()" in js
    assert "audioCtx = null" in js
