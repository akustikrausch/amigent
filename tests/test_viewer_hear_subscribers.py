"""Hear is per page: PCM goes to the pages that asked, for as long as they ask (offline).

Simulated PCM, pages and clock; nothing is played or captured, and no Amiga is involved.
"""
from __future__ import annotations

import functools
import threading
import time
from pathlib import Path

from amigent import viewer as viewer_module
from amigent.channel import PLAIN_CLOSED
from amigent.viewer import AUDIO_PCM_BURST, AUDIO_PCM_RATE, PcmPace, Viewer, pcm_message

APOLLO_AUDIO = {"os": "ApolloOS", "cpu": "68080", "audio": "49", "daemon_protocol": 6}
BLOCK = b"\x01\x00\x02\x00" * 2400  # 50 ms of 48 kHz stereo
ON, OFF = {"t": "audio", "on": True}, {"t": "audio", "on": False}


class Page:
    """An open page. Counts PCM instead of keeping it, so long sessions stay small."""

    def __init__(self, broken=False):
        self.broken = broken
        self.blocks = self.pcm = 0
        self.other = []

    def send(self, data):
        if self.broken:
            raise OSError("the page went away")
        if isinstance(data, bytes) and data[:1] == b"\x05":
            assert data[:5] == pcm_message(48000, b"")
            self.blocks += 1
            self.pcm += len(data) - 5
        else:
            self.other.append(data)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Link:
    """``blocks`` blocks of PCM, each ``speed`` times faster than the mix plays, then the end."""

    def __init__(self, blocks, clock=None, speed=1.0, on_block=None):
        self.left, self.clock, self.speed, self.on_block = blocks, clock, speed, on_block
        self.connected = False
        self.started = self.stopped = self.closed = self.read = 0

    def connect_audio(self, *, tls):
        self.connected = True

    def audio_start(self):
        self.started += 1

    def audio_read(self):
        if not self.connected or self.left <= 0 or self.stopped:
            return None  # after stop, the daemon's end marker
        if self.on_block:
            self.on_block(self.read)
        self.left -= 1
        self.read += 1
        if self.clock:
            self.clock.t += len(BLOCK) / AUDIO_PCM_RATE / self.speed
        return 48000, BLOCK

    def audio_stop(self):
        self.stopped += 1

    def close(self):
        self.closed += 1
        self.connected = False


def machine(*pages):
    viewer = Viewer("192.0.2.10", screen_port=1, ctl_port=1)
    viewer.screen.flags = PLAIN_CLOSED
    viewer.online = True
    viewer.amiga = dict(APOLLO_AUDIO)
    viewer.sent = []
    viewer.broadcast = lambda data: viewer.sent.append(data)
    viewer.event = lambda kind, **fields: viewer.sent.append((kind, fields))
    for page in pages:
        viewer.clients.add(page)
    return viewer


def paced(monkeypatch, clock):
    monkeypatch.setattr(viewer_module, "PcmPace", functools.partial(PcmPace, clock=clock))


def released(viewer, link):
    assert link.started == link.stopped == link.closed == 1
    assert viewer.hearers == set() and not viewer.hear and viewer._audio_idle.is_set()


def test_one_page_with_hear_gets_the_pcm():
    page = Page()
    viewer = machine(page)
    viewer.submit(ON, page)
    link = Link(10)
    viewer._play_audio(link)
    assert page.blocks == 10 and page.pcm == 10 * len(BLOCK)
    assert not [m for m in viewer.sent if isinstance(m, bytes)]  # nothing broadcast
    released(viewer, link)


def test_a_page_without_hear_gets_no_pcm():
    hearing, quiet = Page(), Page()
    viewer = machine(hearing, quiet)
    viewer.submit(ON, hearing)
    link = Link(10)
    viewer._play_audio(link)
    assert hearing.blocks == 10 and quiet.blocks == 0 and quiet.pcm == 0
    released(viewer, link)


def test_two_pages_with_hear_each_get_every_block():
    a, b = Page(), Page()
    viewer = machine(a, b)
    viewer.submit(ON, a)
    viewer.submit(ON, b)
    link = Link(10)
    viewer._play_audio(link)
    assert a.blocks == b.blocks == 10
    released(viewer, link)


def test_turning_hear_off_stops_that_page_and_the_last_one_ends_the_stream():
    a, b = Page(), Page()
    viewer = machine(a, b)
    viewer.submit(ON, a)
    viewer.submit(ON, b)
    steps = {4: (OFF, a), 6: (ON, a), 8: (OFF, a)}

    def toggle(i):
        if i in steps:
            viewer.submit(*steps[i])
        if i == 10:
            viewer.submit(OFF, b)

    link = Link(100, on_block=toggle)
    viewer._play_audio(link)
    assert b.blocks == 10  # blocks 0-9; turning b off before block 10 ended the stream
    assert a.blocks == 4 + 2  # blocks 0-3, then 6-7
    assert link.read == 11  # block 10 was read, but nobody was hearing any more
    released(viewer, link)


def test_a_page_that_closes_or_breaks_is_dropped_and_the_others_keep_listening():
    a, b, broken = Page(), Page(), Page(broken=True)
    viewer = machine(a, b, broken)
    for page in (a, b, broken):
        viewer.submit(ON, page)
    link = Link(10, on_block=lambda i: viewer.leave(a) if i == 3 else None)
    viewer._play_audio(link)
    assert a.blocks == 3 and b.blocks == 10 and broken.blocks == 0
    assert a not in viewer.clients and broken not in viewer.clients and b in viewer.clients
    released(viewer, link)


def test_a_new_page_does_not_inherit_another_pages_hear():
    old, new = Page(), Page()
    viewer = machine(old)
    viewer.submit(ON, old)

    def arrive(i):
        if i == 2:
            viewer.clients.add(new)  # join adds the page to clients, nothing else
        if i == 4:
            viewer.submit(OFF, new)  # its Hear off does not touch the old page

    link = Link(8, on_block=arrive)
    viewer._play_audio(link)
    assert old.blocks == 8 and new.blocks == 0
    released(viewer, link)


def test_hear_from_no_page_or_a_closed_page_subscribes_nothing():
    open_page, gone = Page(), Page()
    viewer = machine(open_page)
    viewer.submit(ON)
    viewer.submit(ON, gone)
    assert viewer.hearers == set() and not viewer._hearing()


def test_listening_outlasts_the_old_44_seconds(monkeypatch):
    clock = Clock()
    paced(monkeypatch, clock)
    page = Page()
    viewer = machine(page)
    viewer.submit(ON, page)
    seconds = 180
    link = Link(seconds * 20, clock=clock)
    viewer._play_audio(link)
    assert page.pcm == seconds * AUDIO_PCM_RATE > 8 * 1024 * 1024 * 3
    assert link.read == seconds * 20 and link.left == 0  # it ended on the daemon's end, not early
    released(viewer, link)


def test_a_link_somewhat_ahead_of_the_mix_is_not_cut_off(monkeypatch):
    clock = Clock()
    paced(monkeypatch, clock)
    page = Page()
    viewer = machine(page)
    viewer.submit(ON, page)
    link = Link(60 * 20, clock=clock, speed=1.5)
    viewer._play_audio(link)
    assert link.left == 0 and page.blocks == 60 * 20


def test_a_daemon_flooding_pcm_is_stopped_with_bounded_output(monkeypatch):
    clock = Clock()
    paced(monkeypatch, clock)
    for speed in (3.0, 1000.0):
        page = Page()
        viewer = machine(page)
        viewer.submit(ON, page)
        link = Link(100000, clock=clock, speed=speed)
        viewer._play_audio(link)
        assert link.left > 0, "a flood was let through to the end"
        played = link.read * len(BLOCK) / AUDIO_PCM_RATE / speed  # seconds the link took
        assert page.pcm <= AUDIO_PCM_BURST + 2 * AUDIO_PCM_RATE * played
        released(viewer, link)


def test_pace_refills_with_time_up_to_the_burst_only():
    clock = Clock()
    pace = PcmPace(clock)
    assert pace.take(AUDIO_PCM_BURST) and not pace.take(4)
    clock.t += 1
    assert pace.take(2 * AUDIO_PCM_RATE) and not pace.take(4)
    clock.t += 3600  # an hour of silence buys no more than the burst
    assert pace.take(AUDIO_PCM_BURST) and not pace.take(4)


def test_repeated_sessions_leave_nothing_behind():
    pages = [Page() for _ in range(3)]
    viewer = machine(*pages)
    threads = threading.active_count()
    for round_ in range(20):
        for page in pages[: 1 + round_ % 3]:
            viewer.submit(ON, page)
        link = Link(5)
        viewer._play_audio(link)
        released(viewer, link)
    for page in pages:
        viewer.leave(page)
    assert viewer.clients == set() and viewer.hearers == set()
    assert threading.active_count() == threads
    assert not [name for name, value in vars(viewer).items()
                if isinstance(value, (list, bytearray)) and name != "sent" and len(value) > 64]


# _media: asleep while nobody hears, awake when a page asks, gone when the viewer retires

def media(monkeypatch):
    """A running _media with its looks counted and the stream replaced by a stand-in."""
    page = Page()
    viewer = machine(page)
    looks, played, playing = [], [], threading.Event()
    real_watching = viewer._watching
    monkeypatch.setattr(viewer, "_watching", lambda: looks.append(1) or real_watching())

    def stream(link, between=None):
        played.append(time.monotonic())
        playing.set()
        while viewer._hearing():
            time.sleep(0.002)
        viewer._deafen()
        playing.clear()

    monkeypatch.setattr(viewer, "_play_audio", stream)
    thread = threading.Thread(target=viewer._media, daemon=True)
    thread.start()
    return viewer, page, thread, looks, played, playing


def settle(looks):
    """Until the thread stops looking (it is asleep), at most a second."""
    deadline = time.monotonic() + 1
    seen = -1
    while time.monotonic() < deadline and seen != len(looks):
        seen = len(looks)
        time.sleep(0.05)
    return len(looks)


def test_media_sleeps_while_nobody_hears_and_wakes_at_once_for_hear(monkeypatch):
    viewer, page, thread, looks, played, playing = media(monkeypatch)
    try:
        asleep = settle(looks)
        time.sleep(0.3)  # the old loop looked every 50 ms here
        assert len(looks) == asleep <= 2
        asked = time.monotonic()
        viewer.submit(ON, page)
        assert playing.wait(1) and played[0] - asked < 0.5
        viewer.submit(OFF, page)
        deadline = time.monotonic() + 1
        while playing.is_set() and time.monotonic() < deadline:
            time.sleep(0.002)
        assert not playing.is_set()
        idle = settle(looks)
        time.sleep(0.3)
        assert len(looks) == idle and len(played) == 1
    finally:
        viewer.retire()
        thread.join(1)
    assert not thread.is_alive()


def test_retire_ends_media_whether_asleep_or_streaming(monkeypatch):
    viewer, page, thread, looks, _, _ = media(monkeypatch)
    settle(looks)
    viewer.retire()
    thread.join(1)
    assert not thread.is_alive()

    viewer, page, thread, _, _, playing = media(monkeypatch)
    viewer.submit(ON, page)
    assert playing.wait(1)
    viewer.retire()
    thread.join(1)
    assert not thread.is_alive() and viewer.hearers == set()


def test_a_wake_during_the_look_is_not_lost(monkeypatch):
    page = Page()
    viewer = machine(page)
    played = threading.Event()
    first = [True]
    real_hearing = viewer._hearing

    def hearing():
        if first[0]:  # the page asks right after this look found nobody hearing
            first[0] = False
            viewer._hear_for(page, True)
            return False
        return real_hearing()

    monkeypatch.setattr(viewer, "_hearing", hearing)
    monkeypatch.setattr(viewer, "_play_audio", lambda link, between=None: (played.set(), viewer._deafen()))
    thread = threading.Thread(target=viewer._media, daemon=True)
    thread.start()
    try:
        assert played.wait(1)
    finally:
        viewer.retire()
        thread.join(1)
    assert not thread.is_alive()


def test_the_page_names_itself_with_each_message():
    source = (Path(viewer_module.__file__).parent / "webserver.py").read_text(encoding="utf-8")
    assert "self.viewer.submit(json.loads(message), ws)" in source
    media_source = Viewer._media.__code__.co_names
    assert "sleep" not in media_source
