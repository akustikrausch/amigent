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
                    runs = b""
                    left = w * h
                    while left:
                        n = min(left, 256)
                        runs += bytes([n - 1, data[2] & 255, 7, 9])
                        left -= n
                    conn.sendall(struct.pack(">HHI", w, h, len(runs)) + runs)
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
    picture = amiga.capture_rgb(0, 0, 1000, 600)
    assert picture.size == (1000, 600)
    assert picture.getpixel((999, 599))[1:] == (7, 9)
    asked = [struct.unpack(">HHHH", c[2:10]) for c in seen if c[0] == 13]
    assert asked == [(0, 0, 1000, 262), (0, 262, 1000, 262), (0, 524, 1000, 76)]
    assert all(c[1] == 1 for c in seen if c[0] == 13)  # packed


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
        {"kind": "gadget", "window": "w1", "id": "4", "label": "Save", "x": 20, "y": 80, "w": 60, "h": 14,
         "disabled": False, "selected": False, "type": "button"},
        {"kind": "gadget", "window": "w1", "id": "5", "label": "Cancel", "x": 200, "y": 80, "w": 60, "h": 14,
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


def locked_amigentd(key, seen):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(2)

    def run():
        while True:
            conn, _ = listener.accept()
            conn.sendall(b"AMGT" + struct.pack(">HH", 2, 1))
            authed = False
            while len(data := conn.recv(10)) == 10:
                seen.append(data[0])
                if data[0] == 17:
                    given = conn.recv(struct.unpack(">H", data[2:4])[0])
                    authed = given == key
                    conn.sendall(b"\0" if authed else b"\1")
                    if not authed:
                        break
                elif not authed:
                    conn.sendall(b"\1")
                    break
                elif data[0] == 1:
                    conn.sendall(struct.pack(">HHHH", 640, 256, 64, 0))
                else:
                    conn.sendall(b"\0")
            conn.close()

    threading.Thread(target=run, daemon=True).start()
    return listener.getsockname()[1]


def test_key_is_sent_first():
    seen = []
    port = locked_amigentd(b"sesam", seen)
    amiga = Amigentd("127.0.0.1", port, timeout=5, key="sesam")
    amiga.ping()
    assert seen[:3] == [17, 18, 1]  # key, then who we are, then INFO


def test_wrong_or_missing_key_is_explained():
    port = locked_amigentd(b"sesam", [])
    with pytest.raises(AmigentdError, match="refused the key"):
        Amigentd("127.0.0.1", port, timeout=5, key="nope").connect()
    with pytest.raises(AmigentdError, match="wants a key"):
        Amigentd("127.0.0.1", port, timeout=5, key="").connect()


def test_setup_keeps_existing_amigactld_settings():
    from amigent.legacy import allow_script

    script = allow_script("192.168.1.10", "SYS:amigent")
    assert 'Echo >>S:amigactld.conf "ALLOW 192.168.1.10"' in script
    assert "Copy S:amigactld.conf S:amigactld.conf.amigent-bak" in script


def test_picture_is_dropped_when_others_took_frames():
    seen = []
    amiga = fake_amigentd(2, {18: b"\1"}, seen)
    amiga.connect()
    amiga._frame = object()  # pretend we had a picture
    amiga.connect()
    assert amiga._frame is None


def test_ui_click_by_number(monkeypatch):
    fake = FakeScreen(tree=[
        {"kind": "window", "id": "08e7b714", "title": "Amigent"},
        {"kind": "gadget", "window": "08e7b714", "id": "6", "label": "", "x": 992, "y": 574,
         "w": 103, "h": 29, "disabled": False, "selected": False, "type": "other"},
    ])
    monkeypatch.setattr(server, "screen", fake)
    server.amiga_ui_click(number=6, window="Amigent")
    assert fake.calls == [("click", 1043, 588)]


def test_ui_click_takes_the_front_window_when_titles_repeat(monkeypatch):
    gadget = lambda win, x: {"kind": "gadget", "window": win, "id": "0", "label": "close", "x": x,
                             "y": 300, "w": 29, "h": 30, "disabled": False, "selected": False,
                             "type": "sys"}
    fake = FakeScreen(tree=[
        {"kind": "window", "id": "0a000001", "title": "GT Label Test"}, gadget("0a000001", 300),
        {"kind": "window", "id": "0a000002", "title": "GT Label Test"}, gadget("0a000002", 600),
    ])
    monkeypatch.setattr(server, "screen", fake)
    server.amiga_ui_click("close", window="GT Label Test")
    assert fake.calls == [("click", 314, 315)]


def test_refusal_is_not_retried():
    seen = []
    amiga = fake_amigentd(2, {7: b"\1"}, seen)
    amiga.connect()
    before = len(seen)
    assert amiga.char("x") is False  # the keymap can't do it: amigentd answers 1
    assert [c[0] for c in seen[before:]] == [7]


def test_changes_seen_by_anyone_count():
    amiga = fake_amigentd(2, {}, [])
    amiga.changes.append((10.0, 0, 0, 64, 64))
    amiga.width, amiga.height = 640, 256
    assert amiga.changed_since(5.0, 0, 0, 100, 100) == 1
    assert amiga.changed_since(5.0, 200, 0, 50, 50) == 0
    assert amiga.changed_since(11.0) == 0


def test_typing_goes_out_in_one_batch():
    seen = []
    amiga = fake_amigentd(2, {}, seen)
    amiga.connect()
    before = len(seen)
    amiga.type_text("ab\n")
    assert [c[0] for c in seen[before:]] == [7, 7, 5, 5]


def test_unlabelled_gadgets_come_with_a_picture(monkeypatch):
    from PIL import Image

    class Screen(FakeScreen):
        def capture(self, x=0, y=0, w=None, h=None):
            return Image.new("RGB", (w or 640 - x, h or 256 - y), (200, 200, 200))

    fake = Screen(tree=[
        {"kind": "screen", "title": "Workbench", "width": 640, "height": 256},
        {"kind": "window", "id": "w1", "title": "Ask", "active": True, "x": 0, "y": 0, "w": 300, "h": 100},
        {"kind": "gadget", "window": "w1", "id": "4", "label": "", "type": "other", "x": 10, "y": 50,
         "w": 100, "h": 20, "disabled": False, "selected": False},
        {"kind": "gadget", "window": "w1", "id": "5", "label": "OK", "type": "button", "x": 150, "y": 50,
         "w": 100, "h": 20, "disabled": False, "selected": False},
    ])
    monkeypatch.setattr(server, "screen", fake)
    result = server.amiga_ui_tree()
    assert "#4 other ''" in result[0] and len(result) == 2
    assert server.amiga_ui_tree(pictures=False) == [result[0]]


def test_raw_colour_when_packing_does_not_help():
    from amigent.amigentd import unpack_rgb

    raw = bytes(range(12))
    assert unpack_rgb(raw, 4) == raw
    assert unpack_rgb(bytes([2, 1, 2, 3]), 3) == bytes([1, 2, 3]) * 3
