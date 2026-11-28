from types import SimpleNamespace

import pytest

from amigent import server
from amigent.errors import AmigaError


def test_wait_file_distinguishes_missing_from_disk_error(machine_session, monkeypatch):
    code = 205
    def stat(path):
        raise AmigaError("DOS error", 1, dos_code=code)
    monkeypatch.setattr(machine_session, "ctl", SimpleNamespace(stat=stat))
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    assert server.amiga_wait_file("RAM:test", gone=True, timeout=0) == "RAM:test is absent"
    code = 218
    with pytest.raises(AmigaError, match="DOS error"):
        server.amiga_wait_file("RAM:test", gone=True, timeout=0)


def test_wait_crc_polls_selected_file(machine_session, monkeypatch):
    values = iter([0, 1])
    def checksum(path, size):
        assert path == "RAM:out" and size == 3
        return next(values)
    ctl = SimpleNamespace(stat=lambda path: {"type": "file", "size": 3}, checksum=checksum)
    monkeypatch.setattr(machine_session, "ctl", ctl)
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    monkeypatch.setattr(server.time, "sleep", lambda _: None)
    assert "matches CRC-32 00000001" in server.amiga_wait_file("RAM:out", crc32="00000001")
    with pytest.raises(ValueError):
        server.amiga_wait_file("RAM:out", crc32="invalid")


def test_wait_up_uses_pinned_port_and_requires_a_greeting(machine_session, monkeypatch):
    seen = []
    def greet(host, port, *args, **kwargs):
        seen.append((host, port))
        return b"not an Amigent greeting"
    monkeypatch.setattr(server.tls, "wanted", lambda host: True)
    monkeypatch.setattr(server.tls, "greet", greet)
    monkeypatch.setattr(server.net, "greet", lambda *a, **kw: pytest.fail("used plain port"))
    assert "timed out" in server.amiga_wait_up(timeout=0)
    assert seen == [(machine_session.host, machine_session.screen.port)]


def test_channel_keeps_numeric_dos_error_in_any_language():
    from amigent.channel import AmigaChannel
    from fakeamiga import FakeAmiga
    class Missing(FakeAmiga):
        def _dir(self, path, stat):
            return self.fail("Objekt nicht gefunden [205]")
    fake = Missing()
    channel = AmigaChannel("127.0.0.1", fake.port, timeout=3)
    try:
        with pytest.raises(AmigaError, match="Objekt nicht gefunden") as failure:
            channel.stat("RAM:missing")
        assert failure.value.code == 1 and failure.value.dos_code == 205
    finally:
        channel.close()
        fake.close()
