"""Archives unpacked on the PC stay in their folder; a broken disk image ends with an error."""

import struct

import pytest

from amigent import adf, server


@pytest.mark.parametrize("member, parts", [
    ("dir/ok.txt", ["dir", "ok.txt"]),
    ("dir\\ok.txt", ["dir", "ok.txt"]),
    ("..\\..\\evil.txt", ["evil.txt"]),
    ("../../evil.txt", ["evil.txt"]),
    ("C:/Windows/evil.txt", []),
    ("C:evil.txt", []),
    ("DH0:evil", []),
    ("./a/./b", ["a", "b"]),
])
def test_member_names_cannot_leave_the_folder(member, parts):
    assert server._member_parts(member) == parts


def test_a_directory_that_contains_itself_is_an_error():
    disk = adf.Disk.__new__(adf.Disk)
    disk.root = 880
    disk.entries = lambda block: [880]
    header = bytearray(512)
    header[432] = 4
    header[433:437] = b"loop"
    struct.pack_into(">l", header, 508, adf.ST_USERDIR)
    disk.block = lambda key: bytes(header)
    with pytest.raises(adf.AdfError):
        disk.walk()
