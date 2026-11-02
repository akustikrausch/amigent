"""Write a classic Workbench project icon that runs a script through IconX.

Usage: make_icon.py OUT.info
Layout from the icon.library DiskObject structure.
"""

import struct
import sys
from pathlib import Path

WIDTH, HEIGHT, DEPTH = 40, 32, 2
WBPROJECT = 4
NO_ICON_POSITION = 0x80000000


def pixel(x: int, y: int) -> int:
    """Blue box (3) with a white border (2) and a white cross."""
    if x in (0, WIDTH - 1) or y in (0, HEIGHT - 1):
        return 1
    in_bar = 16 <= x < 24 and 6 <= y < 26
    in_beam = 8 <= x < 32 and 12 <= y < 20
    return 2 if in_bar or in_beam else 3


def planes() -> bytes:
    data = bytearray()
    row_bytes = (WIDTH + 15) // 16 * 2
    for plane in range(DEPTH):
        for y in range(HEIGHT):
            row = bytearray(row_bytes)
            for x in range(WIDTH):
                if pixel(x, y) >> plane & 1:
                    row[x // 8] |= 0x80 >> (x % 8)
            data += row
    return bytes(data)


def main() -> None:
    tool = b"C:IconX\0"
    gadget = struct.pack(
        ">IhhHHHHHIIIIIHI", 0, 0, 0, WIDTH, HEIGHT, 5, 3, 1, 1, 0, 0, 0, 0, 0, 1
    )
    disk = struct.pack(">HH", 0xE310, 1) + gadget
    disk += struct.pack(">BxIIIIIII", WBPROJECT, 1, 0, NO_ICON_POSITION, NO_ICON_POSITION, 0, 0, 4096)
    image = struct.pack(">hhHHHIBBI", 0, 0, WIDTH, HEIGHT, DEPTH, 1, 3, 0, 0)
    tool_block = struct.pack(">I", len(tool)) + tool
    Path(sys.argv[1]).write_bytes(disk + image + planes() + tool_block)


if __name__ == "__main__":
    main()
