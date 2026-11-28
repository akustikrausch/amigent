"""Failure and replay at the points where a transfer changes the filesystem."""

import io
import zlib

import pytest

from amigent import files, transfer
from amigent.channel import AmigaChannel
from amigent.errors import AmigaError
from fakeamiga import FakeAmiga


@pytest.fixture
def connection():
    fake = FakeAmiga()
    fake.protocol = 6
    fake.info.update(publish="1", readrange="1", appendat="1")
    ch = AmigaChannel("127.0.0.1", fake.port, timeout=5, key="")
    try:
        yield ch, fake
    finally:
        ch.close()
        fake.close()


@pytest.mark.parametrize("lost", ["append", "publish"])
def test_lost_reply(connection, tmp_path, monkeypatch, lost):
    ch, fake = connection
    data = bytes(range(251)) * 600
    original = b"original destination"
    fake.add("RAM:out", original)
    local = tmp_path / "input"
    local.write_bytes(data)
    received = []
    write = ch.append_at
    publish = ch.publish

    def write_then_drop(path, offset, block):
        assert fake.files["RAM:out"] == original
        write(path, offset, block)
        if lost == "append" and offset and not received:
            received.append("append")
            raise ConnectionResetError("lost append reply")

    def publish_then_drop(stage, target):
        publish(stage, target)
        if lost == "publish" and not received:
            received.append("publish")
            raise ConnectionResetError("lost publish reply")

    monkeypatch.setattr(ch, "append_at", write_then_drop)
    monkeypatch.setattr(ch, "publish", publish_then_drop)
    assert files.put(ch, local, "RAM:out").startswith("copied 1 file")
    assert received == [lost]
    assert fake.files == {"RAM:out": data}
    assert all("ag-" not in path for path in fake.dirs)
    output = tmp_path / "saved"
    assert files.get(ch, "RAM:out", output).startswith("saved ")
    assert output.read_bytes() == data


def test_changed_source_preserves_destination(connection):
    ch, fake = connection
    fake.add("RAM:out", b"original")
    with pytest.raises(AmigaError, match="differs"):
        transfer.send(ch, io.BytesIO(b"new"), "RAM:out", 3, zlib.crc32(b"old"))
    assert fake.files == {"RAM:out": b"original"}


def test_failed_commit_keeps_both_files(connection, monkeypatch):
    ch, fake = connection
    fake.add("RAM:out", b"original")

    def refuse(stage, target):
        raise AmigaError("disk full", 1)

    monkeypatch.setattr(ch, "publish", refuse)
    with pytest.raises(AmigaError, match="disk full.*Inspect RAM:ag-"):
        transfer.send(ch, io.BytesIO(b"new"), "RAM:out", 3, zlib.crc32(b"new"))
    assert fake.files["RAM:out"] == b"original"
    assert b"new" in fake.files.values()


def test_bad_download_keeps_local_file(connection, tmp_path, monkeypatch):
    ch, fake = connection
    fake.add("RAM:in", b"source")
    output = tmp_path / "out"
    output.write_bytes(b"original")
    monkeypatch.setattr(ch, "checksum", lambda path, size: 0)
    with pytest.raises(AmigaError, match="differs"):
        files.get(ch, "RAM:in", output)
    assert output.read_bytes() == b"original"
    assert list(tmp_path.iterdir()) == [output]


def test_range_limits(connection):
    ch, fake = connection
    fake.add("RAM:in", b"abc")
    assert ch.read_range("RAM:in", 1, 1) == b"b"
    assert ch.read_range("RAM:in", 2**31, 100) == b""
    assert ch.read_range("RAM:in", 2**32 - 1, 1) == b""
    with pytest.raises(ValueError):
        ch.read_range("RAM:in", 2**32, 1)
    with pytest.raises(ValueError):
        ch.read_range("RAM:in", 0, 65536)


def test_inline_binary_and_checked_append(connection, monkeypatch):
    import base64
    from amigent import server

    ch, fake = connection
    monkeypatch.setattr(server._session(), "ctl", ch)
    monkeypatch.setattr(server, "_with_ctl", lambda fn: fn())
    data = b"\x00\xffhello"
    encoded = base64.b64encode(data).decode()
    for _ in range(2):
        server.amiga_write_text("RAM:out", encoded, encoding="base64", expected_offset=0)
    assert fake.files["RAM:out"] == data
    assert server.amiga_read_text("RAM:out", encoding="base64", length=2) == "AP8="
    with pytest.raises(AmigaError):
        server.amiga_write_text("RAM:out", "changed", expected_offset=0)
    with pytest.raises(ValueError):
        server.amiga_write_text("RAM:out", "bad!", encoding="base64")
