import socket
import struct
import threading

import pytest

import amigent.server as server
from amigent.amigentd import Amigentd, AmigentdError


def fake_amigentd(version, replies, seen):
    """replies maps a command number to the bytes answered for it."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def run():
        while True:
            conn, _ = listener.accept()
            conn.sendall(b"AMGT" + struct.pack(">HH", version, 0))
            while len(data := conn.recv(10)) == 10:
                seen.append(data)
                if data[0] == 1:
                    conn.sendall(struct.pack(">HHHH", 640, 256, 64, 0))
                elif data[0] == 13:
                    w, h = struct.unpack(">HH", data[6:10])
                    conn.sendall(struct.pack(">HH", w, h) + bytes([data[2] & 255, 7, 9]) * (w * h))
                else:
                    conn.sendall(replies.get(data[0], b"\0"))
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return Amigentd("127.0.0.1", listener.getsockname()[1], timeout=5)


def text(body: str) -> bytes:
    raw = body.encode("iso-8859-1")
    return struct.pack(">I", len(raw)) + raw


def test_screens_and_front():
    title = "Workbench Screen".encode()
    listing = struct.pack(">H", 2)
    listing += struct.pack(">IHHHHH", 0x1234ABCD, 640, 256, 8, 1 | 2, len(title)) + title
    listing += struct.pack(">IHHHHH", 0x00C0FFEE, 320, 256, 6, 8, 0)
    seen = []
    amiga = fake_amigentd(2, {11: listing, 12: b"\0"}, seen)
    screens = amiga.screens()
    assert screens[0]["title"] == "Workbench Screen" and screens[0]["front"] and screens[0]["workbench"]
    assert screens[1]["ham"] and screens[1]["handle"] == 0x00C0FFEE
    assert amiga.screen_to_front(0x00C0FFEE)
    assert struct.unpack(">HH", seen[-1][2:6]) == (0x00C0, 0xFFEE)


def test_colour_region_comes_in_pieces():
    seen = []
    amiga = fake_amigentd(2, {}, seen)
    picture = amiga.capture_rgb(0, 0, 600, 10)
    assert picture.size == (600, 10)
    assert picture.getpixel((0, 0))[1:] == (7, 9)
    asked = [struct.unpack(">HHHH", c[2:10]) for c in seen if c[0] == 13]
    assert asked == [(0, 0, 512, 10), (512, 0, 88, 10)]


def test_relative_move_is_signed():
    seen = []
    amiga = fake_amigentd(2, {}, seen)
    amiga.move_relative(-3, 5)
    assert struct.unpack(">hh", seen[-1][2:6]) == (-3, 5)


def test_old_amigentd_is_named():
    amiga = fake_amigentd(1, {}, [])
    with pytest.raises(AmigentdError, match="protocol 1"):
        amiga.screens()


def test_ui_tree_and_menus():
    tree = ("S\t0x10\t640\t256\tWorkbench Screen\n"
            "W\tw1\t10\t20\t300\t100\ta\tSave file\n"
            "G\tw1\tg1\t20\t80\t60\t14\tbutton\t\tSave\n"
            "G\tw1\tg2\t200\t80\t60\t14\tbutton\td\tCancel\n")
    menus = ("M\t0\t5\t0\t60\t11\te\tProject\n"
             "I\t0\t0\t5\t12\t120\t10\te\tO\tOpen...\n"
             "I\t0\t1\t5\t22\t120\t10\te\t\tSave as\n"
             "U\t0\t1\t0\t125\t22\t80\t10\te\t\tIFF\n")
    amiga = fake_amigentd(2, {15: text(tree), 16: text(menus)}, [])
    items = amiga.ui_tree()
    assert [i["kind"] for i in items] == ["screen", "window", "gadget", "gadget"]
    assert items[3]["disabled"] and items[2]["label"] == "Save"
    entries = amiga.menus()
    assert entries[1]["key"] == "O" and entries[3]["path"] == [0, 1, 0]


class FakeScreen:
    def __init__(self, tree=(), menus=()):
        self.tree, self.menu_list, self.calls = list(tree), list(menus), []

    def ui_tree(self):
        return self.tree

    def menus(self):
        return self.menu_list

    def click(self, x, y, button="left", double=False):
        self.calls.append(("click", x, y))

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def button(self, name, down):
        self.calls.append(("button", name, down))


def test_ui_click_by_label(monkeypatch):
    fake = FakeScreen(tree=[
        {"kind": "window", "id": "w1", "title": "Save file"},
        {"kind": "gadget", "window": "w1", "label": "Save", "x": 20, "y": 80, "w": 60, "h": 14,
         "disabled": False, "selected": False, "type": "button"},
        {"kind": "gadget", "window": "w1", "label": "Cancel", "x": 200, "y": 80, "w": 60, "h": 14,
         "disabled": True, "selected": False, "type": "button"},
    ])
    monkeypatch.setattr(server, "screen", fake)
    server.amiga_ui_click("save")
    assert fake.calls == [("click", 50, 87)]
    with pytest.raises(Exception, match="disabled"):
        server.amiga_ui_click("Cancel")


def test_menu_select_drags_with_the_right_button(monkeypatch):
    entry = lambda kind, path, x, y, text: {"kind": kind, "path": path, "x": x, "y": y, "w": 20,  # noqa: E731
                                            "h": 10, "enabled": True, "checked": False,
                                            "key": "", "text": text}
    fake = FakeScreen(menus=[entry("menu", [0], 0, 0, "Project"),
                             entry("item", [0, 0], 0, 20, "Save as"),
                             entry("sub", [0, 0, 0], 40, 20, "IFF")])
    monkeypatch.setattr(server, "screen", fake)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server.amiga_menu_select("Project/Save as/IFF")
    assert fake.calls == [("move", 10, 5), ("button", "right", True), ("move", 10, 25),
                          ("move", 50, 25), ("button", "right", False)]
