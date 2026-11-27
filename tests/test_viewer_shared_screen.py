"""amiga_viewer lends the MCP session's screen connection to the viewer.

A page closing must leave that connection to the agent; the session closes it.
No Amiga: the connection is one end of a socket pair, time is the feed's own waits.
"""
from __future__ import annotations

import socket
import threading
import time
import types

import pytest

import amigent.server as server
from amigent import screenfeed, sessions
from amigent.viewer import Viewer


class Stop(Exception):
    pass


class Wake:
    """The feed's event and clock, in simulated time: each wait returns at once and
    moves the clock on by its timeout."""

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.now = 1000.0
        self.waited = 0.0

    def monotonic(self):
        return self.now

    def wait(self, timeout=None):
        self.waited += timeout or 0.0
        self.now += timeout or 0.0
        if self.waited > self.seconds:
            raise Stop

    def set(self):
        pass

    def clear(self):
        pass


def _idle(viewer: Viewer, seconds: float, monkeypatch) -> None:
    wake = viewer.feed.wake = Wake(seconds)
    monkeypatch.setattr(screenfeed, "time", types.SimpleNamespace(
        monotonic=wake.monotonic, perf_counter=time.perf_counter, sleep=time.sleep))
    with pytest.raises(Stop):
        viewer.feed.run()


@pytest.fixture
def lent(machine_session, monkeypatch):
    """The session's screen, connected, lent to a viewer through amiga_viewer; and the far end."""
    near, far = socket.socketpair()
    machine_session.screen._sock = near
    monkeypatch.setattr(Viewer, "start", lambda self, port=6810: "http://127.0.0.1:1/?t=x")
    monkeypatch.setattr(server, "open_window", lambda url: None)
    getattr(server.amiga_viewer, "fn", server.amiga_viewer)()
    viewer = machine_session.viewer
    events = []
    viewer.event = lambda kind, **fields: events.append((kind, fields))
    yield machine_session, viewer, near, far, events
    viewer.feed._bell_r.close()
    viewer.feed._bell_w.close()
    far.close()


def test_the_viewer_uses_the_sessions_screen_connection(lent):
    session, viewer, near, _, _ = lent
    assert viewer.screen is session.screen and viewer.feed.screen is session.screen
    assert session.screen._sock is near


def test_closing_the_page_leaves_the_agents_connection_open(lent, monkeypatch):
    session, viewer, near, far, events = lent
    page = object()
    viewer.clients.add(page)
    viewer.feed.online = viewer.online = True
    viewer.hearers.add(page)

    viewer.leave(page)
    _idle(viewer, 30.0, monkeypatch)  # far past the 2 s after which the old feed closed it

    assert not viewer.clients and viewer.unused_for() >= 0.0 and viewer.hear is False
    assert session.screen._sock is near and near.fileno() != -1
    assert not [e for e in events if e[0] == "online" and not e[1].get("online")]
    near.sendall(b"AMGT")  # the agent's next request goes out on the same connection
    assert far.recv(4) == b"AMGT"


@pytest.mark.parametrize("same_amiga", [True, False], ids=["moved", "replaced"])
def test_the_session_still_closes_its_screen(lent, monkeypatch, same_amiga):
    session, viewer, near, _, _ = lent
    monkeypatch.setattr(sessions.machines, "address", lambda host: "192.0.2.99")
    monkeypatch.setattr(sessions.machines, "found_again", lambda host: same_amiga)
    session.refresh_address()
    assert session.screen._sock is None and near.fileno() == -1
    assert (session.viewer is viewer) is same_amiga


class Unreachable:
    """A screen that cannot connect."""

    def __init__(self):
        self._sock = None
        self.listeners = []
        self.closed = 0

    def connect(self):
        raise ConnectionRefusedError("refused")

    def close(self):
        self.closed += 1

    def drop_picture(self):
        pass


def test_a_broken_screen_still_goes_offline(monkeypatch):
    lock = threading.Lock()
    viewer = Viewer("192.0.2.10", screen=Unreachable(), lock=lambda: lock)
    seen = []
    viewer.feed.on_online = lambda online, reason: seen.append((online, reason))
    viewer.clients.add(object())
    try:
        _idle(viewer, 1.0, monkeypatch)
    finally:
        viewer.feed._bell_r.close()
        viewer.feed._bell_w.close()
    assert viewer.screen.closed >= 1 and seen == [(False, "refused")]
