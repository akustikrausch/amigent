import json

import pytest
from PIL import Image

from amigent import server
from amigent.errors import AmigaError


class Screen:
    def __init__(self):
        self.windows = [{"kind": "window", "id": "0001", "title": "Shell", "x": -4, "y": 7,
                         "w": 20, "h": 10}]
        self.boxes = []
        self.front = True

    def screens(self):
        return [{"title": "Workbench", "front": self.front, "handle": 8, "display": True,
                 "width": 320, "height": 256}]

    def ui_tree(self, handle):
        return [{"kind": "screen", "handle": handle}, *self.windows]

    def capture_screen(self, target, box):
        self.boxes.append(box)
        return Image.new("RGB", box[2:], (50, 65, 80))


@pytest.fixture
def device(monkeypatch, machine_session):
    screen = Screen()
    monkeypatch.setattr(machine_session, "screen", screen)
    monkeypatch.setattr(server, "_with_screen", lambda fn: fn())
    monkeypatch.setattr(server, "_paused_note", lambda: "")
    return screen


def test_named_window_uses_visible_bounds(device):
    first = json.loads(server.amiga_screen_diff("checksum", window="Shell")[0])
    assert first["region"] == [0, 7, 16, 10] and len(first["sha256"]) == 64
    device.windows[0]["x"] = 4
    second = json.loads(server.amiga_screen_diff("checksum", window="0001")[0])
    assert second["region"] == [4, 7, 20, 10] and second["sha256"] != first["sha256"]
    note, image = server.amiga_screenshot(window="shell", x=2, width=9, jpeg_quality=85)
    assert "(6,7)" in note and image.data.startswith(b"\xff\xd8")


def test_ambiguous_window_or_wrong_screen_refuses(device):
    device.windows.append(dict(device.windows[0], id="0002"))
    with pytest.raises(AmigaError, match="2 windows match"):
        server.amiga_screen_diff("checksum", window="Shell")
    device.ui_tree = lambda handle: [{"kind": "screen", "handle": 9}]
    with pytest.raises(AmigaError, match="screen changed"):
        server.amiga_screen_diff("checksum", window="0001")
    assert device.boxes == []


def test_background_read_addresses_screen_without_raising_it(device):
    device.front = False
    server.amiga_screen_diff("checksum", screen="Workbench", window="Shell")
    assert device.boxes == [(0, 7, 16, 10)]

def test_wait_uses_a_full_quiet_interval_after_last_change():
    from amigent import watch
    now = [0.0]
    class Clock:
        def clock(self):
            return now[0]
        def observe(self, capture, deadline):
            return capture()
    def sleep(seconds):
        now[0] += seconds
    frames = iter(("a", "a", "b", "b", "b", "b"))
    result = watch.wait_digest(Clock(), lambda: next(frames), 2, quiet=0.75, sleep=sleep)
    assert now[0] == 1.25 and result.endswith("sha256=b")
