"""1.6.0 (#2): a program without the OS (WHDLoad) is not reported as a crash."""

import struct
import time

import pytest

from amigent import noos, paths


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "FOLDER", tmp_path)


@pytest.mark.parametrize("command, program, slave", [
    ("WHDLoad Lemmings.slave", "WHDLoad: Lemmings", "Lemmings.slave"),
    ('C:WHDLoad SLAVE="Data2:Games/Turrican II/Turrican2.Slave" PRELOAD', "WHDLoad: Turrican2",
     "Data2:Games/Turrican II/Turrican2.Slave"),
    ("cd Data2:Games/Pinball\nwhdload", "WHDLoad", ""),
    ("Data2:Games/Lotus/Lotus.slave", "WHDLoad: Lotus", "Data2:Games/Lotus/Lotus.slave"),
])
def test_whdload_starts_are_recognised(command, program, slave):
    assert noos.detect(command) == {"program": program, "slave": slave}


@pytest.mark.parametrize("command", ["List SYS:", "Copy whdloadfaq.txt RAM:", "Run SYS:Utilities/MultiView"])
def test_other_commands_are_not(command):
    assert noos.detect(command) is None


def slave(keyexit: int) -> bytes:
    # hunk header, then the slave: moveq/rts, "WHDLOADS", version, flags, ..., keydebug, keyexit
    hunk = struct.pack(">IIIIII", 0x3F3, 0, 1, 0, 0, 10) + struct.pack(">I", 0x3E9) + struct.pack(">I", 10)
    head = b"\x70\xff\x4e\x75" + b"WHDLOADS" + struct.pack(">HHIIHHH", 17, 0, 0x80000, 0, 0, 0, 0)
    return hunk + head + bytes([0x58, keyexit]) + b"\0" * 8


def test_the_quit_key_comes_from_the_slave():
    assert noos.slave_quit_key(slave(0x59)) == 0x59
    assert noos.key_name(0x59) == "F10" and noos.key_name(0x5D) == "numeric pad *"
    assert noos.slave_quit_key(slave(0)) is None
    assert noos.slave_quit_key(b"not a slave") is None


def test_a_note_lives_until_the_amiga_answers_after_being_gone():
    host = "192.0.2.7"
    noos.mark(host, {"program": "WHDLoad: Lemmings", "slave": ""}, 0x59)
    noos.answered(host)  # still loading: the Amiga answers before the game takes over
    note = noos.gone(host)
    assert note["gone"] and note["quit_key"] == "F10"
    text = noos.explain(note)
    assert "without the operating system" in text and "F10" in text and "don't cut the power" in text
    noos.answered(host)
    assert noos.current(host) is None


def test_a_program_that_never_took_over_is_forgotten(monkeypatch):
    host = "192.0.2.8"
    noos.mark(host, {"program": "WHDLoad", "slave": ""})
    later = time.time() + noos.SETTLE + 1
    monkeypatch.setattr(noos.time, "time", lambda: later)
    noos.answered(host)
    assert noos.current(host) is None


def test_an_old_note_expires(monkeypatch):
    host = "192.0.2.9"
    noos.mark(host, {"program": "WHDLoad", "slave": ""})
    later = time.time() + noos.KEEP + 1
    monkeypatch.setattr(noos.time, "time", lambda: later)
    assert noos.current(host) is None


def test_power_and_restart_ask_first(monkeypatch):
    import amigent.server as server

    class S:
        host = "192.0.2.10"
        ctl = None
    monkeypatch.setattr(server, "_session", lambda: S)
    assert server._held_by_no_os() == ""
    noos.mark(S.host, {"program": "WHDLoad: Lemmings", "slave": ""})
    held = server._held_by_no_os()
    assert held.startswith("not done:") and "force=True" in held
