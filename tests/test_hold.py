"""1.3.0: menus and popups that hold Intuition up, restarts, typing line by line."""

import amigent.server as server
from amigent import uptime
from test_protocol2 import fake_amigentd, text


def test_held_comes_from_the_screen_line():
    new = fake_amigentd(2, {15: text("S\t0x10\t640\t256\tWorkbench Screen\th\n")}, [])
    assert new.held() == "held"
    menu = fake_amigentd(2, {15: text("S\t0x10\t640\t256\tWorkbench Screen\tm\n")}, [])
    assert menu.held() == "menu"
    old = fake_amigentd(2, {15: text("S\t0x10\t640\t256\tWorkbench Screen\n")}, [])
    assert old.held() == ""  # amigentd before 1.3.0 doesn't say


class HoldScreen:
    def __init__(self, states, right=False):
        self.states, self.right, self.calls = list(states), right, []

    def held(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def right_held(self):
        return self.right

    def press(self, combo):
        self.calls.append(("press", combo))

    def button(self, name, down):
        self.calls.append(("button", name, down))


def test_popup_is_closed_before_a_start(monkeypatch):
    fake = HoldScreen(["held", ""])
    ran = []
    monkeypatch.setattr(server, "screen", fake)
    monkeypatch.setattr(server, "_with_ctl", lambda fn: ran.append(fn) or None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    said = server.amiga_start("NewShell")
    assert fake.calls == [("press", "esc")]
    assert said.startswith("closed a popup with Esc first.") and said.endswith("started: NewShell")
    assert ran


def test_a_menu_someone_holds_is_left_alone(monkeypatch):
    fake = HoldScreen(["menu"])
    monkeypatch.setattr(server, "screen", fake)
    assert "A menu is open" in server._clear_hold()
    assert fake.calls == []  # letting go of a button we didn't press could pick an item


def test_our_own_right_button_is_let_go(monkeypatch):
    fake = HoldScreen(["menu", ""], right=True)
    monkeypatch.setattr(server, "screen", fake)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    assert server._clear_hold() == "let go of the right mouse button first. "
    assert fake.calls == [("button", "right", False)]


def test_nothing_held_says_nothing(monkeypatch):
    monkeypatch.setattr(server, "screen", HoldScreen([""]))
    assert server._clear_hold() == ""


def test_restart_is_told_from_the_uptimes():
    before = {"at": 1000.0, "uptime": 5000, "daemon_uptime": 4990}
    assert uptime.notes({"uptime": "5600", "daemon_uptime": "5590"}, before, 1600.0) == []
    rebooted = uptime.notes({"uptime": "120", "daemon_uptime": "100"}, before, 1600.0)
    assert len(rebooted) == 1 and rebooted[0].startswith("the Amiga started again 2 min ago")
    updated = uptime.notes({"uptime": "5600", "daemon_uptime": "30"}, before, 1600.0)
    assert updated and updated[0].startswith("amigentd started again 30 s ago")
    assert uptime.notes({"uptime": "5600"}, None, 1600.0) == []  # nothing to compare with
    # an emulator whose clock runs slow: 300 s shown after 480 s, from 31 s
    slow = {"at": 0.0, "uptime": 31, "daemon_uptime": 20}
    assert uptime.notes({"uptime": "300", "daemon_uptime": "290"}, slow, 480.0) == []
    # a crash soon after a look still shows: 120 s up after 480 s, from 31 s
    assert uptime.notes({"uptime": "120", "daemon_uptime": "100"}, slow, 480.0)


def test_last_alert_is_named():
    said = uptime.notes({"uptime": "60", "last_alert": "8000000b"}, None, 0.0)
    assert "8000000B" in said[0]


def test_seen_is_kept_between_sessions(tmp_path):
    first = uptime.Seen(tmp_path / "uptime.json")
    assert first.check("A1200", {"uptime": "9000", "daemon_uptime": "8990"}, now=100.0) == []
    again = uptime.Seen(tmp_path / "uptime.json")
    assert again.check("A1200", {"uptime": "30", "daemon_uptime": "20"}, now=200.0)
    assert again.check("A1200", {"uptime": "130", "daemon_uptime": "120"}, now=300.0) == []


def test_old_amigentd_without_uptime_gives_no_notes(tmp_path):
    seen = uptime.Seen(tmp_path / "uptime.json")
    assert seen.check("A500", {"chip_free": "1"}) == []
    assert not (tmp_path / "uptime.json").exists()


def test_duration_reads_well():
    assert uptime.duration(45) == "45 s"
    assert uptime.duration(600) == "10 min"
    assert uptime.duration(3 * 3600 + 120) == "3 h 2 min"
    assert uptime.duration(3 * 86400) == "3 days 0 h"


class TypeScreen:
    def __init__(self, polls=()):
        self.typed, self.polls = [], list(polls)

    def ui_tree(self):
        return [{"kind": "window", "active": True, "x": 10, "y": 20, "w": 300, "h": 100}]

    def type_text(self, text):
        self.typed.append(text)

    def poll(self, x, y, w, h):
        assert (x, y, w, h) == (10, 20, 300, 100)
        return self.polls.pop(0) if self.polls else 0


def test_lines_go_in_one_by_one(monkeypatch):
    fake = TypeScreen()
    monkeypatch.setattr(server, "screen", fake)
    monkeypatch.setattr(server, "SETTLE_FIRST", 0)
    monkeypatch.setattr(server, "SETTLE_QUIET", 0.01)
    said = server.amiga_type("Echo a\nWait 2\nEcho b\n")
    assert fake.typed == ["Echo a\n", "Wait 2\n", "Echo b\n"]
    assert said == "typed 21 characters in 3 lines"


def test_a_window_that_keeps_changing_is_reported(monkeypatch):
    fake = TypeScreen(polls=[5] * 10000)
    monkeypatch.setattr(server, "screen", fake)
    monkeypatch.setattr(server, "SETTLE_FIRST", 0)
    said = server.amiga_type("List\nEcho b\n", settle=0.05)
    assert fake.typed == ["List\n", "Echo b\n"] and "1 line(s) went on" in said


def test_one_line_or_settle_zero_types_at_once(monkeypatch):
    fake = TypeScreen()
    monkeypatch.setattr(server, "screen", fake)
    server.amiga_type("Echo a\n")
    server.amiga_type("a\nb\n", settle=0)
    assert fake.typed == ["Echo a\n", "a\nb\n"]
