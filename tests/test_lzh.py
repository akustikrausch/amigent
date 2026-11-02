from pathlib import Path

import pytest

from amigent import lzh

SAMPLE = Path(__file__).parent.parent / "bootstrap" / "amigactld-0.8.2.lha"


def test_stored_member():
    body = b"hello amiga"
    name = b"amigent/hi"
    head = b"-lh0-" + len(body).to_bytes(4, "little") * 2 + bytes(4) + b"\x20\x00"
    head += bytes([len(name)]) + name + bytes(2)
    archive = bytes([len(head), sum(head) & 255]) + head + body + b"\0"
    assert lzh.extract(archive, "/hi") == body


@pytest.mark.skipif(not SAMPLE.exists(), reason="sample archive not present")
def test_real_archive_unpacks_a_hunk_executable():
    binary = lzh.extract(SAMPLE.read_bytes(), "/amigactld")
    assert binary[:4] == b"\x00\x00\x03\xf3" and len(binary) == 117848
