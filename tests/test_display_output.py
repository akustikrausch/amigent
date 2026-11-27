"""1.6.0 (#1): which video output the picture is on, with the screen's mode."""

import pytest
from PIL import Image

import amigent.server as server
from amigent import display
from amigent.amigentd import Amigentd

from fakeamiga import FakeAmiga

KNOWN = 0x8000
WB, GAME = 0x0A001000, 0x0A002000
PAL_HIRES_LACE = 0x00029004


def screen(handle, title, front=False, rtg=False, mode=None, name="", chip=False):
    s = {"handle": handle, "title": title, "width": 640, "height": 256, "depth": 8, "front": front,
         "workbench": title == "Workbench", "rtg": rtg, "ham": False, "display": True,
         "chip_shown": chip, "in_view": chip, "foreign_view": False}
    if mode is not None:
        s.update(mode=mode, mode_name=name)
    return s


def chipset_in_front():
    return [screen(GAME, "Game", front=True, mode=PAL_HIRES_LACE, name="PAL:HighRes Laced", chip=True),
            screen(WB, "Workbench", rtg=True, mode=0x50001000, name="VideoCore:1920x1080")]


def test_a_chipset_screen_in_front_of_a_card_moves_the_picture():
    st = display.state(chipset_in_front())
    assert st == {"output": "chipset", "mode": "PAL:HighRes Laced", "title": "Game", "card": True,
                  "moved": True, "limits": ["planar"]}
    line = display.where(chipset_in_front())
    assert "Amiga's own video output" in line and "(PAL:HighRes Laced)" in line and "HDMI" in line


def test_without_a_card_the_chipset_is_the_only_output():
    found = [screen(WB, "Workbench", front=True, mode=PAL_HIRES_LACE, chip=True)]
    assert not display.state(found)["moved"] and display.where(found) == ""


def test_the_card_in_front_says_nothing():
    found = list(reversed(chipset_in_front()))
    found[0]["front"], found[1]["front"] = True, False
    assert display.state(found)["output"] == "card" and display.where(found) == ""


def test_dual_playfield_is_named_as_a_limit():
    found = chipset_in_front()
    found[0]["mode"] |= display.DUALPF_KEY
    assert display.state(found)["limits"] == ["planar", "dualpf"]
    assert "dual playfield" in display.where(found)


def test_a_mode_without_a_name_shows_its_number():
    assert display.mode_label({"mode": 0x29004, "mode_name": ""}) == "mode 0x00029004"
    assert display.mode_label({}) == ""


def test_the_screenshot_note_carries_the_output_and_the_mode():
    note = server.display_note(chipset_in_front())
    assert "Amiga's own video output" in note
    assert "'Workbench' (640x256, graphics card, VideoCore:1920x1080" in note


@pytest.fixture
def amiga():
    fake = FakeAmiga(Image.new("RGB", (320, 128), (170, 170, 170)))
    yield fake
    fake.close()


def test_screens_bring_mode_and_name(amiga):
    amiga.screen_list = [
        {"handle": GAME, "title": "Game", "flags": 1 | 16 | 32 | KNOWN, "mode": PAL_HIRES_LACE,
         "mode_name": "PAL:HighRes Laced"},
        {"handle": WB, "title": "Workbench", "flags": 2 | 4 | KNOWN, "mode": 0x50001000, "mode_name": ""},
    ]
    found = Amigentd("127.0.0.1", amiga.port, timeout=5).screens()
    assert found[0]["mode"] == PAL_HIRES_LACE and found[0]["mode_name"] == "PAL:HighRes Laced"
    assert found[1]["mode_name"] == "" and found[1]["title"] == "Workbench"


def test_an_older_amigentd_has_no_mode(amiga):
    found = Amigentd("127.0.0.1", amiga.port, timeout=5).screens()
    assert "mode" not in found[0] and display.mode_label(found[0]) == ""
