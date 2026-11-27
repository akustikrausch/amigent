"""Hear is per machine: stop-before-update and independent Viewer state (offline, not hardware)."""
from __future__ import annotations

import threading
import time
from contextlib import nullcontext
from types import SimpleNamespace

from amigent import server
from amigent.channel import PLAIN_CLOSED
from amigent.viewer import Viewer


class Page:
    """An open page: what the viewer sent it."""

    def __init__(self):
        self.got = []

    def send(self, data):
        self.got.append(data)


def _page(host: str) -> Viewer:
    viewer = Viewer(host, screen_port=1, ctl_port=1)
    viewer.screen.flags = PLAIN_CLOSED
    viewer.online = True
    viewer.page = Page()
    viewer.clients.add(viewer.page)
    viewer.sent = []
    viewer.broadcast = lambda data: viewer.sent.append(data)
    viewer.event = lambda kind, **fields: viewer.sent.append((kind, fields))
    viewer.amiga = {"os": "ApolloOS", "cpu": "68080", "audio": "49", "daemon_protocol": 6}
    return viewer


class Link:
    """An audio link that keeps producing PCM until the viewer stops asking."""

    def __init__(self):
        self.connected = False
        self.started = self.stopped = self.closed = 0

    def connect_audio(self, *, tls):
        self.connected = True

    def audio_start(self):
        self.started += 1

    def audio_read(self):
        if self.stopped:
            return None  # like amigentd: the end marker follows the stop request
        time.sleep(0.005)
        return 48000, b"\x01\x00\x02\x00"

    def audio_stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1
        self.connected = False


def test_two_viewers_have_independent_hear_state():
    a, b = _page("192.0.2.10"), _page("192.0.2.11")
    assert a.hear is False and b.hear is False
    a.submit({"t": "audio", "on": True}, a.page)
    assert a.hear is True and b.hear is False
    b.submit({"t": "audio", "on": True}, b.page)
    a.submit({"t": "audio", "on": False}, a.page)
    assert a.hear is False and b.hear is True


def test_retiring_one_machine_does_not_clear_the_other():
    a, b = _page("192.0.2.10"), _page("192.0.2.11")
    a.submit({"t": "audio", "on": True}, a.page)
    b.submit({"t": "audio", "on": True}, b.page)
    a.retire()
    assert a.hear is False and a.enabled is False
    assert b.hear is True and b.enabled is True and b._hearing()
    assert ("audio", {"on": False}) not in b.sent


def test_one_machine_going_offline_does_not_clear_the_other():
    a, b = _page("192.0.2.10"), _page("192.0.2.11")
    a.submit({"t": "audio", "on": True}, a.page)
    b.submit({"t": "audio", "on": True}, b.page)
    a._set_online(False, "")
    assert a.hear is False and a.amiga == {}
    assert b.hear is True and b.online is True and b.amiga["audio"] == "49"


def test_last_page_leaving_one_machine_leaves_the_other_hearing():
    a, b = _page("192.0.2.10"), _page("192.0.2.11")
    a.submit({"t": "audio", "on": True}, a.page)
    b.submit({"t": "audio", "on": True}, b.page)
    a.leave(next(iter(a.clients)))
    assert a.hear is False and b.hear is True and b._hearing()


def test_stop_hear_on_one_machine_leaves_the_other():
    a, b = _page("192.0.2.10"), _page("192.0.2.11")
    a.submit({"t": "audio", "on": True}, a.page)
    b.submit({"t": "audio", "on": True}, b.page)
    assert a.stop_hear() is True
    assert a.hear is False and b.hear is True


def test_stop_hear_clears_hear_tells_the_page_and_keeps_the_machine():
    viewer = _page("192.0.2.10")
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    assert viewer.stop_hear() is True
    assert viewer.hear is False and not viewer._hearing()
    assert ("audio", {"on": False}) in viewer.sent
    assert viewer.enabled is True and viewer.online is True and viewer.reason == ""
    assert viewer.amiga["audio"] == "49"


def test_stop_hear_when_hear_is_off_is_quiet():
    viewer = _page("192.0.2.10")
    assert viewer.stop_hear() is False
    assert viewer.sent == []


def test_stop_hear_waits_for_a_running_stream_to_release_its_link():
    viewer = _page("192.0.2.10")
    viewer.submit({"t": "audio", "on": True}, viewer.page)
    link = Link()
    thread = threading.Thread(target=viewer._play_audio, args=(link,), daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while link.started == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert link.started == 1 and viewer._hearing()
    assert viewer.stop_hear(wait=2) is True
    assert link.stopped == 1 and link.closed == 1 and viewer.hear is False
    thread.join(2)
    assert not thread.is_alive()


def _session(host, viewer, order):
    class Closer:
        def __init__(self, name):
            self.name = name

        def close(self):
            order.append((host, self.name + ".close"))

    return SimpleNamespace(host=host, viewer=viewer, calls=threading.RLock(),
                           ctl=Closer("ctl"), screen=Closer("screen"), lock_file=f"{host}.lock")


def _registry(monkeypatch, *sessions):
    monkeypatch.setattr(server.sessions, "_sessions", {s.host: s for s in sessions})
    monkeypatch.setattr(server, "_exclusive", lambda: nullcontext())
    monkeypatch.setattr(server, "_screen_lock", lambda: nullcontext())


def test_close_for_update_clears_hear_on_every_open_session_before_closing(monkeypatch):
    order = []
    a, b = _page("192.0.2.10"), _page("192.0.2.11")
    for viewer in (a, b):
        viewer.submit({"t": "audio", "on": True}, viewer.page)
    for host, viewer in (("192.0.2.10", a), ("192.0.2.11", b)):
        real = viewer.stop_hear
        viewer.stop_hear = lambda wait=2.0, host=host, real=real: (order.append((host, "stop_hear")), real(wait))[1]
    first, second = _session("192.0.2.10", a, order), _session("192.0.2.11", b, order)
    _registry(monkeypatch, first, second)

    server._close_for_update()

    assert a.hear is False and b.hear is False
    assert a.enabled is True and b.enabled is True
    for host in ("192.0.2.10", "192.0.2.11"):
        names = [n for h, n in order if h == host]
        assert names == ["stop_hear", "ctl.close", "screen.close"]


def test_close_for_update_without_a_viewer_still_closes(monkeypatch):
    order = []
    session = _session("192.0.2.10", None, order)
    _registry(monkeypatch, session)
    server._close_for_update()
    assert order == [("192.0.2.10", "ctl.close"), ("192.0.2.10", "screen.close")]


def test_close_for_update_a_failing_stop_hear_does_not_block_the_close(monkeypatch):
    order = []
    viewer = _page("192.0.2.10")

    def boom(wait=2.0):
        raise RuntimeError("stuck")

    viewer.stop_hear = boom
    _registry(monkeypatch, _session("192.0.2.10", viewer, order))
    server._close_for_update()
    assert order == [("192.0.2.10", "ctl.close"), ("192.0.2.10", "screen.close")]
