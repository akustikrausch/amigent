"""PAUSE: the Amiga stops reading its screen for a while, input and commands go on."""

import threading
import time

import pytest

from amigent import screenfeed, server
from amigent.amigentd import Amigentd
from amigent.channel import AmigaChannel, Commands
from amigent.errors import AmigaError
from amigent.screenfeed import Feed, InputQueue
from fakeamiga import FakeAmiga


@pytest.fixture
def amiga():
    a = FakeAmiga()
    yield a
    a.close()


def until(test, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not test():
        assert time.monotonic() < deadline
        time.sleep(0.02)


def test_a_channel_pauses_and_resumes(amiga):
    ch = AmigaChannel("127.0.0.1", amiga.port, 5)
    assert ch.pause_screen(60) == 60
    assert ch.screen_paused() in (59, 60)
    assert ch.pause_screen(0) == 0
    assert ch.pause_screen(10 ** 6) == 65535  # what INFO's u16 can tell


def test_the_screen_hears_of_a_pause_in_info(amiga):
    amiga.set_pause(30)
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    assert screen.pause_left() in (29, 30)
    frames = amiga.counts.get(2, 0)
    assert screen.pause(0) == 0
    assert screen.pause(20) == 20
    assert amiga.counts.get(2, 0) == frames  # pausing asks for no picture


def test_an_older_amigentd_has_no_pause(amiga):
    amiga.protocol = 3
    with pytest.raises(AmigaError, match="protocol 3"):
        AmigaChannel("127.0.0.1", amiga.port, 5).pause_screen(60)
    assert Amigentd("127.0.0.1", amiga.port, timeout=5).pause_left() == 0


def test_the_feed_asks_for_no_frames_while_paused_but_input_goes_through(amiga, monkeypatch):
    monkeypatch.setattr(screenfeed, "PAUSE_LOOK", 0.3)
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    lock = threading.Lock()
    inputs = InputQueue()
    feed = Feed(screen, lambda: lock, inputs)
    threading.Thread(target=feed.run, daemon=True).start()
    until(lambda: screen.width and not screen.missing_tiles())
    amiga.set_pause(30)
    until(lambda: screen.paused_until > time.monotonic())  # an INFO with the next sweep
    time.sleep(0.3)  # a sweep that was already out finishes
    frames, moves = amiga.counts.get(2, 0), amiga.counts.get(3, 0)
    inputs.put({"t": "move", "x": 5, "y": 5})
    feed.ring()
    time.sleep(1.0)
    assert amiga.counts.get(2, 0) == frames
    assert amiga.counts.get(3, 0) == moves + 1
    amiga.set_pause(0)  # ended early, from somewhere else
    until(lambda: amiga.counts.get(2, 0) > frames, 3)


def test_the_tools_stand_in_a_note_for_the_picture(amiga, monkeypatch):
    ctl = Commands("127.0.0.1", 1, amiga.port, timeout=5)
    screen = Amigentd("127.0.0.1", amiga.port, timeout=5)
    monkeypatch.setattr(server, "ctl", ctl)
    monkeypatch.setattr(server, "screen", screen)
    monkeypatch.setattr(server, "_exclusive", lambda name=None: threading.Lock())
    assert server.amiga_pause_screen(120).startswith("screen paused for 120 s")
    shot = server.amiga_screenshot()
    assert len(shot) == 1 and "paused" in shot[0]
    assert "paused" in server.amiga_wait_change(timeout=1)
    assert server.amiga_pause_screen(0) == "screen pause ended"
    assert len(server.amiga_screenshot()) == 2
