"""An Amiga that was updated on its own side tells the PC that it is behind."""
from amigent import channel, server


def test_same_or_older_amigentd_says_nothing():
    assert channel.pc_outdated(channel.NEWEST_PROTOCOL) == ""
    assert channel.pc_outdated(3) == ""


def test_a_newer_amigentd_asks_for_the_pc_update():
    text = channel.pc_outdated(channel.NEWEST_PROTOCOL + 1)
    assert "pipx upgrade amigent" in text and "newer" in text


class _Backend:
    _sock = object()
    protocol = channel.NEWEST_PROTOCOL + 1


def test_the_agent_hears_it_once(monkeypatch):
    monkeypatch.setattr(server.ctl, "backend", _Backend())
    monkeypatch.setattr(server, "_outdated_told", False)
    first = server._outdated_note()
    assert first.startswith("Note: amigentd on the Amiga is newer")
    assert server._outdated_note() == ""


def test_nothing_before_the_link_is_up(monkeypatch):
    monkeypatch.setattr(server.ctl, "backend", None)
    monkeypatch.setattr(server, "_outdated_told", False)
    assert server._outdated_note() == ""
    assert server._outdated_told is False  # asks again once connected
